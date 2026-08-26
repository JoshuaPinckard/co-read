package focalmap

import (
	"reflect"
	"testing"
)

func TestParseUnifiedZero(t *testing.T) {
	diff := []byte(`diff --git a/p/x_test.go b/p/x_test.go
index 1..2 100644
--- a/p/x_test.go
+++ b/p/x_test.go
@@ -10,2 +10,3 @@
-old
+new
diff --git a/p/x.go b/p/x.go
--- a/p/x.go
+++ b/p/x.go
@@ -1 +1 @@
`)
	got, err := ParseUnifiedZero(diff)
	if err != nil {
		t.Fatal(err)
	}
	want := []FileDiff{{
		OldPath: "p/x_test.go",
		NewPath: "p/x_test.go",
		Hunks: []Hunk{{
			Old: Range{Start: 10, Count: 2},
			New: Range{Start: 10, Count: 3},
		}},
	}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v, want %#v", got, want)
	}
}

func TestFunctionsAndNestedSubtestMapping(t *testing.T) {
	source := []byte(`package p

func helper() {}
func TestTable(t *testing.T) {
	cases := []string{"a", "b"}
	for _, tc := range cases {
		t.Run(tc, func(t *testing.T) {})
	}
}
func (T) TestMethod() {}
func TestMain(m *testing.M) {}
func BenchmarkX(b *testing.B) {}
`)
	functions, err := Functions("x_test.go", source)
	if err != nil {
		t.Fatal(err)
	}
	if len(functions) != 1 || functions[0].Name != "TestTable" {
		t.Fatalf("functions = %#v", functions)
	}
	got := NamesForRange(functions, Range{Start: 7, Count: 1})
	if !reflect.DeepEqual(got, []string{"TestTable"}) {
		t.Fatalf("nested row mapped to %v", got)
	}
	if got := NamesForRange(functions, Range{Start: 3, Count: 1}); len(got) != 0 {
		t.Fatalf("helper-only hunk mapped to %v", got)
	}
}

func TestAnchoredRegexp(t *testing.T) {
	got := AnchoredRegexp([]string{"TestB", "TestA/odd"})
	want := `^(?:TestA/odd|TestB)$`
	if got != want {
		t.Fatalf("got %q, want %q", got, want)
	}
}
