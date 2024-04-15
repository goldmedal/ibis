"""Lower the ibis expression graph to a SQL-like relational algebra."""

from __future__ import annotations

import operator
from functools import reduce
from typing import TYPE_CHECKING, Any

import toolz
from public import public

import ibis.common.exceptions as com
import ibis.expr.datatypes as dt
import ibis.expr.operations as ops
from ibis.common.annotations import attribute
from ibis.common.collections import FrozenDict  # noqa: TCH001
from ibis.common.deferred import var
from ibis.common.graph import Graph
from ibis.common.patterns import InstanceOf, Object, Pattern, _, replace
from ibis.common.typing import VarTuple  # noqa: TCH001
from ibis.expr.rewrites import d, p, replace_parameter
from ibis.expr.schema import Schema

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

x = var("x")
y = var("y")


@public
class CTE(ops.Relation):
    """Common table expression."""

    parent: ops.Relation

    @attribute
    def schema(self):
        return self.parent.schema

    @attribute
    def values(self):
        return self.parent.values


@public
class Select(ops.Relation):
    """Relation modelled after SQL's SELECT statement."""

    parent: ops.Relation
    selections: FrozenDict[str, ops.Value] = {}
    predicates: VarTuple[ops.Value[dt.Boolean]] = ()
    sort_keys: VarTuple[ops.SortKey] = ()

    @attribute
    def values(self):
        return self.selections

    @attribute
    def schema(self):
        return Schema({k: v.dtype for k, v in self.selections.items()})


@public
class FirstValue(ops.Analytic):
    """Retrieve the first element."""

    arg: ops.Column[dt.Any]

    @attribute
    def dtype(self):
        return self.arg.dtype


@public
class LastValue(ops.Analytic):
    """Retrieve the last element."""

    arg: ops.Column[dt.Any]

    @attribute
    def dtype(self):
        return self.arg.dtype


# TODO(kszucs): there is a better strategy to rewrite the relational operations
# to Select nodes by wrapping the leaf nodes in a Select node and then merging
# Project, Filter, Sort, etc. incrementally into the Select node. This way we
# can have tighter control over simplification logic.


@replace(p.Project)
def project_to_select(_, **kwargs):
    """Convert a Project node to a Select node."""
    return Select(_.parent, selections=_.values)


@replace(p.Filter)
def filter_to_select(_, **kwargs):
    """Convert a Filter node to a Select node."""
    return Select(_.parent, selections=_.values, predicates=_.predicates)


@replace(p.Sort)
def sort_to_select(_, **kwargs):
    """Convert a Sort node to a Select node."""
    return Select(_.parent, selections=_.values, sort_keys=_.keys)


@replace(p.WindowFunction(p.First | p.Last))
def first_to_firstvalue(_, **kwargs):
    """Convert a First or Last node to a FirstValue or LastValue node."""
    if _.func.where is not None:
        raise com.UnsupportedOperationError(
            f"`{type(_.func).__name__.lower()}` with `where` is unsupported "
            "in a window function"
        )
    klass = FirstValue if isinstance(_.func, ops.First) else LastValue
    return _.copy(func=klass(_.func.arg))


def complexity(node):
    """Assign a complexity score to a node.

    Subsequent projections can be merged into a single projection by replacing
    the fields referenced in the outer projection with the computed expressions
    from the inner projection. This inlining can result in very complex value
    expressions depending on the projections. In order to prevent excessive
    inlining, we assign a complexity score to each node.

    The complexity score assigns 1 to each value expression and adds up in the
    tree hierarchy unless there is a Field node where we don't add up the
    complexity of the referenced relation. This way we treat fields kind of like
    reusable variables considering them less complex than they were inlined.
    """

    def accum(node, *args):
        if isinstance(node, ops.Field):
            return 1
        else:
            return 1 + sum(args)

    return node.map_nodes(accum)[node]


@replace(Object(Select, Object(Select)))
def merge_select_select(_, **kwargs):
    """Merge subsequent Select relations into one.

    This rewrites eliminates `_.parent` by merging the outer and the inner
    `predicates`, `sort_keys` and keeping the outer `selections`. All selections
    from the inner Select are inlined into the outer Select.
    """
    # don't merge if either the outer or the inner select has window functions
    blocking = (
        ops.WindowFunction,
        ops.ExistsSubquery,
        ops.InSubquery,
        ops.Unnest,
        ops.Impure,
    )
    if _.find_below(blocking, filter=ops.Value):
        return _
    if _.parent.find_below(blocking, filter=ops.Value):
        return _

    subs = {ops.Field(_.parent, k): v for k, v in _.parent.values.items()}
    selections = {k: v.replace(subs, filter=ops.Value) for k, v in _.selections.items()}

    predicates = tuple(p.replace(subs, filter=ops.Value) for p in _.predicates)
    unique_predicates = toolz.unique(_.parent.predicates + predicates)

    sort_keys = tuple(s.replace(subs, filter=ops.Value) for s in _.sort_keys)
    sort_key_exprs = {s.expr for s in sort_keys}
    parent_sort_keys = tuple(
        k for k in _.parent.sort_keys if k.expr not in sort_key_exprs
    )
    unique_sort_keys = sort_keys + parent_sort_keys

    result = Select(
        _.parent.parent,
        selections=selections,
        predicates=unique_predicates,
        sort_keys=unique_sort_keys,
    )
    return result if complexity(result) <= complexity(_) else _


def extract_ctes(node):
    result = []
    cte_types = (Select, ops.Aggregate, ops.JoinChain, ops.Set, ops.Limit, ops.Sample)
    dont_count = (ops.Field, ops.CountStar, ops.CountDistinctStar)

    g = Graph.from_bfs(node, filter=~InstanceOf(dont_count))
    for node, dependents in g.invert().items():
        if isinstance(node, ops.View) or (
            len(dependents) > 1 and isinstance(node, cte_types)
        ):
            result.append(node)

    return result


def sqlize(
    node: ops.Node,
    params: Mapping[ops.ScalarParameter, Any],
    rewrites: Sequence[Pattern] = (),
) -> tuple[ops.Node, list[ops.Node]]:
    """Lower the ibis expression graph to a SQL-like relational algebra.

    Parameters
    ----------
    node
        The root node of the expression graph.
    params
        A mapping of scalar parameters to their values.
    rewrites
        Supplementary rewrites to apply to the expression graph.

    Returns
    -------
    Tuple of the rewritten expression graph and a list of CTEs.

    """
    assert isinstance(node, ops.Relation)

    # apply the backend specific rewrites
    if rewrites:
        node = node.replace(reduce(operator.or_, rewrites))

    # lower the expression graph to a SQL-like relational algebra
    context = {"params": params}
    sqlized = node.replace(
        replace_parameter
        | project_to_select
        | filter_to_select
        | sort_to_select
        | first_to_firstvalue,
        context=context,
    )

    # squash subsequent Select nodes into one
    simplified = sqlized.replace(merge_select_select)

    # extract common table expressions while wrapping them in a CTE node
    ctes = frozenset(extract_ctes(simplified))

    def wrap(node, _, **kwargs):
        new = node.__recreate__(kwargs)
        return CTE(new) if node in ctes else new

    result = simplified.replace(wrap)
    ctes = reversed([cte.parent for cte in result.find(CTE)])

    return result, ctes


# supplemental rewrites selectively used on a per-backend basis

"""Replace `log2` and `log10` with `log`."""
replace_log2 = p.Log2 >> d.Log(_.arg, base=2)
replace_log10 = p.Log10 >> d.Log(_.arg, base=10)


"""Add an ORDER BY clause to rank window functions that don't have one."""


@replace(p.WindowFunction(func=p.NTile(y), order_by=()))
def add_order_by_to_empty_ranking_window_functions(_, **kwargs):
    return _.copy(order_by=(y,))


"""Replace checks against an empty right side with `False`."""
empty_in_values_right_side = p.InValues(options=()) >> d.Literal(False, dtype=dt.bool)


@replace(
    p.WindowFunction(p.RankBase | p.NTile)
    | p.StringFind
    | p.FindInSet
    | p.ArrayPosition
)
def one_to_zero_index(_, **kwargs):
    """Subtract one from one-index functions."""
    return ops.Subtract(_, 1)


@replace(ops.NthValue)
def add_one_to_nth_value_input(_, **kwargs):
    if isinstance(_.nth, ops.Literal):
        nth = ops.Literal(_.nth.value + 1, dtype=_.nth.dtype)
    else:
        nth = ops.Add(_.nth, 1)
    return _.copy(nth=nth)


@replace(p.Capitalize)
def rewrite_capitalize(_, **kwargs):
    """Rewrite Capitalize in terms of substring, concat, upper, and lower."""
    first = ops.Uppercase(ops.Substring(_.arg, start=0, length=1))
    # use length instead of length - 1 to avoid backends complaining about
    # asking for negative length
    #
    # there are at most length - 1 characters, so asking for length is fine
    rest = ops.Lowercase(ops.Substring(_.arg, start=1, length=ops.StringLength(_.arg)))
    return ops.StringConcat((first, rest))


@replace(p.Sample)
def rewrite_sample_as_filter(_, **kwargs):
    """Rewrite Sample as `t.filter(random() <= fraction)`.

    Errors as unsupported if a `seed` is specified.
    """
    if _.seed is not None:
        raise com.UnsupportedOperationError(
            "`Table.sample` with a random seed is unsupported"
        )
    return ops.Filter(_.parent, (ops.LessEqual(ops.RandomScalar(), _.fraction),))


@replace(p.WindowFunction(order_by=()))
def rewrite_empty_order_by_window(_, **kwargs):
    return _.copy(order_by=(ops.NULL,))


@replace(p.WindowFunction(p.RowNumber | p.NTile))
def exclude_unsupported_window_frame_from_row_number(_, **kwargs):
    return ops.Subtract(_.copy(start=None, end=0), 1)


@replace(p.WindowFunction(p.MinRank | p.DenseRank, start=None))
def exclude_unsupported_window_frame_from_rank(_, **kwargs):
    return ops.Subtract(
        _.copy(start=None, end=0, order_by=_.order_by or (ops.NULL,)), 1
    )


@replace(
    p.WindowFunction(
        p.Lag | p.Lead | p.PercentRank | p.CumeDist | p.Any | p.All, start=None
    )
)
def exclude_unsupported_window_frame_from_ops(_, **kwargs):
    return _.copy(start=None, end=0, order_by=_.order_by or (ops.NULL,))
