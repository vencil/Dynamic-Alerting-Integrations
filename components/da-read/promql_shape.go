package main

import (
	"encoding/json"
	"fmt"

	"github.com/prometheus/prometheus/promql/parser"
)

// `da-read promql-shape -`: the tree of each PromQL expression as Prometheus'
// parser builds it (#4097) — every aggregation with its `by` / `without`
// labels, every binary operator with its vector matching (`on` / `ignoring`,
// `group_left` / `group_right` and the labels they carry), every call and
// every selector, each holding its operands. A check that asks "which labels
// does this rule group or join on" off the text is one spelling behind: it
// does not tell `by (a)` from `without (a)`, an aggregation's `by` from a
// `by` in a label value, or which side of a join a selector feeds.
//
// Standard input is a JSON array of expressions; stdout is
//
//	{"documents": [[<node>, …]]}
//
// one node per expression, in input order. A node is one of
//
//	{"kind": "aggregate", "op", "without": bool, "labels": [...], "expr": <node>, "param": <node>|null}
//	{"kind": "binary", "op", "bool": bool, "matching": null | {"card", "on": bool, "labels": [...],
//	                                                            "include": [...]}, "lhs": <node>, "rhs": <node>}
//	{"kind": "call", "name", "args": [<node>, …]}
//	{"kind": "selector", "name", "matchers": [{"name", "type", "value"}, …], "range": bool}
//	{"kind": "subquery" | "paren" | "unary", "expr": <node>}        ("unary" also has "op")
//	{"kind": "number" | "string"}
//
// and every node has "start" / "end", byte offsets into the expression's
// UTF-8 encoding. "matching" is null when the operator has no vector on one
// side (`x > 5`); its "card" is Prometheus' own spelling ("one-to-one",
// "many-to-one" for group_left, "one-to-many" for group_right,
// "many-to-many" for the set operators). A selector's "name" is the metric
// name it is written with, "" for `{__name__=~"…"}`; "range" says it is a
// range selector (`x[5m]`). A matcher's "type" is "=", "!=", "=~" or "!~".
// A node kind this does not know is a refusal, not a hole in the tree: a
// reader that skipped what it did not recognise would report the rule as
// having no grouping there.
//
// An expression Prometheus' parser refuses refuses the input (rc 2, the
// refusal document), as in promql-selectors; standard input that is not a
// JSON array of strings is the caller's error.
func readPromQLShape(exprs []string) ([]byte, error) {
	out := make([]*shapeNode, 0, len(exprs))
	for i, e := range exprs {
		node, err := parseExpr(e)
		if err != nil {
			return nil, fmt.Errorf("expression %d: %w", i, err)
		}
		s, err := shapeOf(node)
		if err != nil {
			return nil, fmt.Errorf("expression %d: %w", i, err)
		}
		out = append(out, s)
	}
	return json.Marshal(struct {
		Documents [][]*shapeNode `json:"documents"`
	}{Documents: [][]*shapeNode{out}})
}

// shapeNode is one node of the printed tree. Fields a kind does not have
// are left out of its JSON; "labels", "include", "args" and "matchers" are
// always arrays where the kind has them.
type shapeNode struct {
	Kind  string `json:"kind"`
	Start int    `json:"start"`
	End   int    `json:"end"`

	Op      string     `json:"op,omitempty"`
	Without *bool      `json:"without,omitempty"`
	Labels  *[]string  `json:"labels,omitempty"`
	Expr    *shapeNode `json:"expr,omitempty"`
	Param   *shapeParm `json:"param,omitempty"`

	Bool     *bool          `json:"bool,omitempty"`
	Matching *shapeMatchOpt `json:"matching,omitempty"`
	LHS      *shapeNode     `json:"lhs,omitempty"`
	RHS      *shapeNode     `json:"rhs,omitempty"`

	Name     *string         `json:"name,omitempty"`
	Args     *[]*shapeNode   `json:"args,omitempty"`
	Matchers *[]shapeMatcher `json:"matchers,omitempty"`
	Range    *bool           `json:"range,omitempty"`
}

// shapeParm prints an aggregation's parameter as the node or null — a
// pointer field with omitempty could not tell "no parameter" from "absent
// key", and the key is part of the aggregate's shape.
type shapeParm struct{ node *shapeNode }

func (p shapeParm) MarshalJSON() ([]byte, error) { return json.Marshal(p.node) }

// shapeMatchOpt prints a binary operator's matching as the object or null.
type shapeMatchOpt struct{ m *shapeMatching }

func (o shapeMatchOpt) MarshalJSON() ([]byte, error) { return json.Marshal(o.m) }

type shapeMatching struct {
	Card    string   `json:"card"`
	On      bool     `json:"on"`
	Labels  []string `json:"labels"`
	Include []string `json:"include"`
}

type shapeMatcher struct {
	Name  string `json:"name"`
	Type  string `json:"type"`
	Value string `json:"value"`
}

// The kinds of shapeNode.
const (
	shapeAggregate = "aggregate"
	shapeBinary    = "binary"
	shapeCall      = "call"
	shapeSelector  = "selector"
	shapeSubquery  = "subquery"
	shapeParen     = "paren"
	shapeUnary     = "unary"
	shapeNumber    = "number"
	shapeString    = "string"
)

func strs(in []string) []string {
	out := make([]string, len(in))
	copy(out, in)
	return out
}

// shapeOf is the tree under one parsed node.
func shapeOf(n parser.Expr) (*shapeNode, error) {
	pr := n.PositionRange()
	s := &shapeNode{Start: int(pr.Start), End: int(pr.End)}
	sub := func(e parser.Expr) (*shapeNode, error) { return shapeOf(e) }
	switch x := n.(type) {
	case *parser.AggregateExpr:
		s.Kind, s.Op = shapeAggregate, x.Op.String()
		without, labels := x.Without, strs(x.Grouping)
		s.Without, s.Labels = &without, &labels
		expr, err := sub(x.Expr)
		if err != nil {
			return nil, err
		}
		s.Expr = expr
		s.Param = &shapeParm{}
		if x.Param != nil {
			p, err := sub(x.Param)
			if err != nil {
				return nil, err
			}
			s.Param.node = p
		}
	case *parser.BinaryExpr:
		s.Kind, s.Op = shapeBinary, x.Op.String()
		b := x.ReturnBool
		s.Bool = &b
		s.Matching = &shapeMatchOpt{}
		if x.VectorMatching != nil {
			s.Matching.m = &shapeMatching{
				Card:    x.VectorMatching.Card.String(),
				On:      x.VectorMatching.On,
				Labels:  strs(x.VectorMatching.MatchingLabels),
				Include: strs(x.VectorMatching.Include),
			}
		}
		lhs, err := sub(x.LHS)
		if err != nil {
			return nil, err
		}
		rhs, err := sub(x.RHS)
		if err != nil {
			return nil, err
		}
		s.LHS, s.RHS = lhs, rhs
	case *parser.Call:
		s.Kind = shapeCall
		name := x.Func.Name
		s.Name = &name
		args := make([]*shapeNode, 0, len(x.Args))
		for _, a := range x.Args {
			an, err := sub(a)
			if err != nil {
				return nil, err
			}
			args = append(args, an)
		}
		s.Args = &args
	case *parser.MatrixSelector:
		vs, ok := x.VectorSelector.(*parser.VectorSelector)
		if !ok {
			return nil, fmt.Errorf("a range selector over %T", x.VectorSelector)
		}
		shapeSelectorInto(s, vs, true)
	case *parser.VectorSelector:
		shapeSelectorInto(s, x, false)
	case *parser.SubqueryExpr:
		s.Kind = shapeSubquery
		expr, err := sub(x.Expr)
		if err != nil {
			return nil, err
		}
		s.Expr = expr
	case *parser.ParenExpr:
		s.Kind = shapeParen
		expr, err := sub(x.Expr)
		if err != nil {
			return nil, err
		}
		s.Expr = expr
	case *parser.UnaryExpr:
		s.Kind, s.Op = shapeUnary, x.Op.String()
		expr, err := sub(x.Expr)
		if err != nil {
			return nil, err
		}
		s.Expr = expr
	case *parser.NumberLiteral:
		s.Kind = shapeNumber
	case *parser.StringLiteral:
		s.Kind = shapeString
	default:
		return nil, fmt.Errorf("a %T node, which promql-shape does not print", n)
	}
	return s, nil
}

func shapeSelectorInto(s *shapeNode, vs *parser.VectorSelector, ranged bool) {
	s.Kind = shapeSelector
	name := vs.Name
	s.Name = &name
	ms := make([]shapeMatcher, 0, len(vs.LabelMatchers))
	for _, m := range vs.LabelMatchers {
		ms = append(ms, shapeMatcher{Name: m.Name, Type: m.Type.String(), Value: m.Value})
	}
	s.Matchers = &ms
	s.Range = &ranged
}
