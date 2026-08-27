package main

import (
	"reflect"
	"strings"
	"testing"
)

func TestParseEventsNormalizesTimingOutputAndOrder(t *testing.T) {
	raw := []byte(`{"Time":"later","Action":"run","Package":"example/p","Test":"TestTable"}
{"Action":"output","Package":"example/p","Test":"TestTable/one","Output":"detail 12ms\\n"}
{"Action":"pass","Package":"example/p","Test":"TestTable/one","Elapsed":9.1}
{"Action":"pass","Package":"example/p","Test":"TestTable","Elapsed":10.2}
`)
	got, err := parseEvents("./p", raw)
	if err != nil {
		t.Fatal(err)
	}
	want := []normalizedCase{
		{Package: "./p", Test: "TestTable", Outcome: "pass"},
		{Package: "./p", Test: "TestTable/one", Outcome: "pass"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v, want %#v", got, want)
	}
}

func TestTFACCClassificationAndMissingParents(t *testing.T) {
	raw := []byte(`{"Action":"output","Test":"TestAccCloud","Output":"set TF_ACC to run acceptance tests\\n"}
{"Action":"skip","Test":"TestAccCloud"}
{"Action":"pass","Test":"TestUnit"}
`)
	events, err := parseEvents("./p", raw)
	if err != nil {
		t.Fatal(err)
	}
	observed, missing, guarded, passing := classifyParents([]string{"TestAccCloud", "TestMissing", "TestUnit"}, events)
	if !reflect.DeepEqual(observed, []string{"TestAccCloud", "TestUnit"}) {
		t.Fatalf("observed = %v", observed)
	}
	if !reflect.DeepEqual(missing, []string{"TestMissing"}) {
		t.Fatalf("missing = %v", missing)
	}
	if !reflect.DeepEqual(guarded, []string{"TestAccCloud"}) {
		t.Fatalf("guarded = %v", guarded)
	}
	if passing != 1 {
		t.Fatalf("passing = %d", passing)
	}
}

func TestGateEnvironmentRejectsReservedOverridesAndPinsOfflineRoutes(t *testing.T) {
	for _, entry := range []string{"GOPROXY=direct", "goflags=-x", "TF_ACC=1", "GONOPROXY=example.com"} {
		if _, err := gateEnvironment(nil, []string{entry}, "-vet=off", "off"); err == nil {
			t.Fatalf("reserved override %q was accepted", entry)
		}
	}
	if _, err := gateEnvironment(nil, []string{"GOCACHE=one", "gocache=two"}, "-vet=off", "off"); err == nil {
		t.Fatal("duplicate environment assignment was accepted")
	}
	environment, err := gateEnvironment(nil, []string{"GOCACHE=C:/cache"}, "-vet=off", "off")
	if err != nil {
		t.Fatal(err)
	}
	joined := strings.Join(environment, "\n")
	for _, expected := range []string{"GOCACHE=C:/cache", "GOFLAGS=-vet=off", "GOPROXY=off", "GONOPROXY=none", "GOVCS=*:off"} {
		if !strings.Contains(joined, expected) {
			t.Fatalf("environment missing %q: %v", expected, environment)
		}
	}
}

func TestValidateMappingRequiresExactAnchoredRegexp(t *testing.T) {
	parentOne := mappingPackage{
		Package:          "./p",
		MappedNames:      []string{"TestA"},
		BasePresentNames: []string{"TestA"},
		RunRegexp:        "^(?:TestA)$",
	}
	parentTwo := mappingPackage{
		Package:         "./p",
		MappedNames:     []string{"TestB"},
		BaseAbsentNames: []string{"TestB"},
	}
	valid := mapping{SchemaVersion: 1, Parents: []mappingParent{
		{Parent: "parent-one", Packages: []mappingPackage{parentOne}},
		{Parent: "parent-two", Packages: []mappingPackage{parentTwo}},
	}, Packages: []mappingPackage{{
		Package:          "./p",
		MappedNames:      []string{"TestA", "TestB"},
		BasePresentNames: []string{"TestA"},
		BaseAbsentNames:  []string{"TestB"},
		RunRegexp:        "^(?:TestA)$",
	}}}
	if err := validateMapping(valid); err != nil {
		t.Fatal(err)
	}
	valid.Packages[0].RunRegexp = "TestA"
	if err := validateMapping(valid); err == nil {
		t.Fatal("broad mapping regexp was accepted")
	}
}

func TestUnrunnableParentsRequiresBasePresentNamePerSide(t *testing.T) {
	value := mapping{Parents: []mappingParent{
		{Parent: "runnable", Packages: []mappingPackage{{BasePresentNames: []string{"TestA"}}}},
		{Parent: "added-only", Packages: []mappingPackage{{BaseAbsentNames: []string{"TestB"}}}},
	}}
	if got := unrunnableParents(value); !reflect.DeepEqual(got, []string{"added-only"}) {
		t.Fatalf("unrunnable parents = %v", got)
	}
}
