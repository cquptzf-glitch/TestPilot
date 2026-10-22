"""Deterministic sample cases served by the local acceptance lab."""
from copy import deepcopy

WINDOW_PREFIX = "TestPilot Mock Instrument"


def _step(action, **values):
    return {"action": action, **values}


def _state(path, expect, timeout=2000):
    return _step("assert.state", args={"path": path, "expect": expect},
                 verify={"type": "state", "timeout_ms": timeout})


def _command(cmd, **args):
    return _step("device.call", args={"cmd": cmd, "args": args})


def _target(key, name, kind):
    return {"key": key, "primary": {"type": "uia", "name": name, "control_type": kind}}


def drafts(http_port=19380):
    normal = [_command("start_measurement", sample_id="SAMPLE-001"),
              _state("/state/status", "Completed"),
              _state("/state/value", "42.5")]
    ui = [_step("app.launch", args={"title_re": f"^{WINDOW_PREFIX} - {http_port}$"}),
          _step("ui.input", target=_target("edit.sample", "Sample ID", "Edit"),
                value="UI-001", args={"clear": True}),
          _step("ui.select", target=_target("combo.mode", "Measurement mode", "ComboBox"),
                value="Precision"),
          _step("ui.click", target=_target("btn.measure", "Start measurement", "Button")),
          _state("/state/status", "Completed"),
          _state("/state/sample_id", "UI-001"),
          _state("/state/mode", "Precision"),
          _step("assert.ui", verify={"type": "text", "expect": "Completed", "op": "equals",
                "target": {"type": "uia", "name": "Completed", "control_type": "Text"},
                "timeout_ms": 3000}),
          _step("ui.click", target=_target("btn.export", "Export report", "Button")),
          _state("/state/export_count", 1)]
    cases = [
        (91001, "Device measurement completes", ["scene:normal"], normal),
        (91002, "Wrong value must fail", ["scene:wrong_value"], normal),
        (91003, "Delayed result is polled", ["scene:slow"],
         [_command("start_measurement", sample_id="SLOW-001"),
          _state("/state/status", "Completed", 4000)]),
        (91004, "Device error must fail", ["scene:device_error"],
         [_command("start_measurement", sample_id="ERROR-001")]),
        (91005, "Missing scene must block", ["scene:missing_scene"], normal),
        (91006, "Native UI input select measure export", ["scene:normal"], ui),
        (91007, "Long wait can be aborted", ["scene:normal"],
         [_step("sleep", args={"ms": 10000})]),
    ]
    out = {}
    for cid, title, preconditions, steps in cases:
        numbered = []
        for i, step in enumerate(steps, 1):
            step = deepcopy(step)
            step.update(id=f"s{i}", source_text=f"{title}: step {i}")
            numbered.append(step)
        out[cid] = {"case_id": f"TC-{cid}", "zentao_case_id": cid, "title": title,
                    "preconditions": preconditions, "steps": numbered,
                    "notes": "LOCAL SIMULATION: deterministic fixture, not an AI prediction."}
    return out


def raw_cases(http_port=19380):
    return {cid: {"id": cid, "product": 1, "module": 10, "title": case["title"],
                  "lastEditedDate": "2026-10-15T12:00:00",
                  "steps": [{"desc": step["source_text"], "expect": ""}
                            for step in case["steps"]]}
            for cid, case in drafts(http_port).items()}
