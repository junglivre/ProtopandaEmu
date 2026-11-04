#!/usr/bin/env python3
import argparse, json, pathlib, re, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

def main():
    ap = argparse.ArgumentParser(description="Structurally validate the ProtopandaEmu G0 decision evidence")
    ap.add_argument("decision"); ap.add_argument("risk_register"); ap.add_argument("adr"); ap.add_argument("backend_audit")
    args = ap.parse_args()
    paths = [pathlib.Path(x) for x in (args.decision, args.risk_register, args.adr, args.backend_audit)]
    errors = []
    if any(not p.is_file() or not p.read_text(errors="replace").strip() for p in paths): errors.append("one or more G0 documents are missing or empty")
    decision = paths[0].read_text(errors="replace") if paths[0].is_file() else ""
    risk = paths[1].read_text(errors="replace") if paths[1].is_file() else ""
    adr = paths[2].read_text(errors="replace") if paths[2].is_file() else ""
    audit = paths[3].read_text(errors="replace") if paths[3].is_file() else ""
    states = re.findall(r"^GATE:\s*(GO|NO-GO|INCOMPLETE|NOT_EXECUTED)\s*$", decision, re.M)
    if len(states) != 1: errors.append("decision must contain exactly one GATE: GO/NO-GO/INCOMPLETE/NOT_EXECUTED line")
    invalid_go = [line for line in decision.splitlines() if re.match(r"^GO:\s*", line) and line.strip() not in ("GO: esp-emulator", "GO: qemu")]
    if invalid_go: errors.append("invalid GO backend: " + invalid_go[0])
    if states and states[0] in {"GO", "NO-GO", "INCOMPLETE", "NOT_EXECUTED"} and not re.search(r"^Plan B:\s*\S", decision, re.M):
        errors.append("non-GO gate must name a non-empty Plan B")
    dimensions = ["coverage", "extensibility", "host-bridge viability", "wall_clock_to_first_uart_byte_ms", "backend-audit.md"]
    for term in dimensions:
        if term.lower() not in decision.lower(): errors.append(f"decision dimension missing: {term}")
    for evidence_path in ("artifacts/esp-emulator-boot.json", "artifacts/qemu-boot.json", "artifacts/esp-emulator-hub75-extension.json", "artifacts/qemu-hub75-extension.json", "artifacts/hostdir-spike-none.json", "docs/backend-audit.md"):
        if evidence_path not in decision and evidence_path not in audit:
            errors.append(f"evidence path not cited: {evidence_path}")
    if states and states[0] == "GO":
        go_match = re.search(r"^GO:\s*(esp-emulator|qemu)\s*$", decision, re.M)
        if not go_match:
            errors.append("GATE: GO requires an exact GO: esp-emulator or GO: qemu line")
            backend = None
        else:
            backend = go_match.group(1)
        if backend is None:
            backend_files = []
        else:
            backend_files = [f"artifacts/{backend}-boot.json", f"artifacts/{backend}-hub75-extension.json", f"artifacts/hostdir-spike-{backend}.json"]
        for item in backend_files:
            p = pathlib.Path(item)
            if not p.is_file(): errors.append(f"GO evidence missing: {item}"); continue
            checked = subprocess.run([sys.executable, str(ROOT / "scripts/validate-spike-result.py"), str(p)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if checked.returncode != 0:
                errors.append(f"GO evidence failed spike validation: {item}: {checked.stderr.decode(errors='replace').strip()}")
            else:
                try: status = json.loads(p.read_text()).get("status")
                except Exception: status = None
                if status != "PASS": errors.append(f"GO evidence is not PASS: {item} ({status})")
    for title in ("Context", "Alternatives", "Decision", "Consequences/Reversibility"):
        match = re.search(rf"^##\s+{re.escape(title)}\s*$", adr, re.M)
        if not match: errors.append(f"ADR section missing: {title}")
        else:
            body = adr[match.end():]; body = body[:re.search(r"^##\s+", body, re.M).start()] if re.search(r"^##\s+", body, re.M) else body
            if not body.strip(): errors.append(f"ADR section empty: {title}")
    for backend in ("esp-emulator", "qemu"):
        section = re.search(rf"^##\s+{re.escape(backend)}\s*$", audit, re.M)
        if not section: errors.append(f"backend audit section missing: {backend}"); continue
        body = audit[section.end():]; body = body[:re.search(r"^##\s+", body, re.M).start()] if re.search(r"^##\s+", body, re.M) else body
        for field in ("Repository URL", "Pinned commit", "License", "Last-commit date", "Maintenance-cost note"):
            if field.lower() not in body.lower(): errors.append(f"backend audit field missing: {backend}/{field}")
    for i in range(1, 17):
        if not re.search(rf"^\|\s*R{i:02d}\s*\|", risk, re.M): errors.append(f"risk row missing: R{i:02d}")
    if errors:
        for error in errors: print("FAIL: " + error)
        return 1
    if not states:
        return 1
    if states[0] in {"INCOMPLETE", "NOT_EXECUTED"}:
        print(f"G0 documents structurally valid; gate status={states[0]} (G0 is not passed)")
        return 3
    print(f"G0 documents structurally valid; gate status={states[0]}")
    return 0

if __name__ == "__main__": raise SystemExit(main())
