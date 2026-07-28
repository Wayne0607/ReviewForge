from __future__ import annotations

from reviewforge.engine.sibling_invariants import (
    analyze_sibling_invariants,
    findings_from_sibling_invariants,
)


def _go_patch(*added: tuple[int, str]) -> str:
    start = min(line for line, _text in added)
    end = max(line for line, _text in added)
    body = "\n".join(f"+{text}" for _line, text in added)
    return f"@@ -{start},{end - start + 1} +{start},{end - start + 1} @@\n{body}\n"


def test_detects_enriched_logger_alias_bypass_supported_by_siblings():
    content = """package rest

func (d *Writer) Create(ctx Context) {
    log := d.Log.WithValues("method", "create")
    ctx = klog.NewContext(ctx, log)
}

func (d *Writer) Update(ctx Context) {
    log := d.Log.WithValues("method", "update")
    ctx = klog.NewContext(ctx, log)
}

func (d *Writer) DeleteCollection(ctx Context) {
    log := d.Log.WithValues("method", "delete-collection")
    ctx = klog.NewContext(ctx, log)
}

func (d *Writer) Delete(ctx Context) {
    log := d.Log.WithValues("method", "delete")
    ctx = klog.NewContext(ctx, d.Log)
}
"""
    target_line = next(
        index for index, line in enumerate(content.splitlines(), start=1) if "NewContext(ctx, d.Log)" in line
    )
    invariants = analyze_sibling_invariants(
        content,
        "writer.go",
        _go_patch((target_line, "    ctx = klog.NewContext(ctx, d.Log)")),
    )

    assert len(invariants) == 1
    invariant = invariants[0]
    assert invariant.kind == "enriched-alias-bypass"
    assert invariant.actual == "d.Log"
    assert invariant.expected == "log"
    assert set(invariant.support_symbols) >= {"Create", "Update"}

    findings = findings_from_sibling_invariants({"sibling_invariants": [invariant.to_dict()]})
    assert len(findings) == 1
    assert findings[0].category == "missing-context-field"
    assert findings[0].verified_by == "detector-sibling-invariant"


def test_does_not_flag_base_logger_without_two_independent_siblings():
    content = """package rest

func (d *Writer) Create(ctx Context) {
    log := d.Log.WithValues("method", "create")
    ctx = klog.NewContext(ctx, log)
}

func (d *Writer) Delete(ctx Context) {
    log := d.Log.WithValues("method", "delete")
    ctx = klog.NewContext(ctx, d.Log)
}

func helper() {}
"""
    target_line = next(
        index for index, line in enumerate(content.splitlines(), start=1) if "NewContext(ctx, d.Log)" in line
    )

    assert not analyze_sibling_invariants(
        content,
        "writer.go",
        _go_patch((target_line, "    ctx = klog.NewContext(ctx, d.Log)")),
    )


def test_detects_unique_changed_telemetry_argument_outlier():
    content = """package rest

func (d *Writer) Create(options Options) {
    d.recordStorageDuration(false, mode, options.Kind, "create", start)
}
func (d *Writer) Update(options Options) {
    d.recordStorageDuration(false, mode, options.Kind, "update", start)
}
func (d *Writer) List(options Options) {
    d.recordStorageDuration(false, mode, options.Kind, "list", start)
}
func (d *Writer) Delete(name string, options Options) {
    d.recordStorageDuration(false, mode, name, "delete", start)
}
"""
    target_line = next(index for index, line in enumerate(content.splitlines(), start=1) if "mode, name," in line)
    invariants = analyze_sibling_invariants(
        content,
        "writer.go",
        _go_patch(
            (
                target_line,
                '    d.recordStorageDuration(false, mode, name, "delete", start)',
            )
        ),
    )

    match = next(item for item in invariants if item.kind == "telemetry-argument-outlier" and item.argument_index == 2)
    assert match.actual == "name"
    assert match.expected == "options.Kind"


def test_telemetry_outlier_requires_the_outlier_to_be_added():
    content = """package rest

func (d *Writer) Create(options Options) {
    d.recordStorageDuration(false, mode, options.Kind, "create", start)
}
func (d *Writer) Update(options Options) {
    d.recordStorageDuration(false, mode, options.Kind, "update", start)
}
func (d *Writer) List(options Options) {
    d.recordStorageDuration(false, mode, options.Kind, "list", start)
}
func (d *Writer) Delete(name string, options Options) {
    d.recordStorageDuration(false, mode, name, "delete", start)
}
"""
    create_line = next(index for index, line in enumerate(content.splitlines(), start=1) if '"create"' in line)

    assert not analyze_sibling_invariants(
        content,
        "writer.go",
        _go_patch(
            (
                create_line,
                '    d.recordStorageDuration(false, mode, options.Kind, "create", start)',
            )
        ),
    )
