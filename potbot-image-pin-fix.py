from pathlib import Path
import subprocess

root = Path(subprocess.check_output(['git','rev-parse','--show-toplevel'], text=True).strip())
p = root / 'src/potbot/wsl_core_runtime.py'
t = root / 'tests/unit/test_windows_core_runtime.py'
a = p.read_bytes(); b = t.read_bytes()
def edit(raw, pairs):
    nl = '\r\n' if b'\r\n' in raw else '\n'
    s = raw.decode('utf-8').replace('\r\n','\n')
    for old,new in pairs:
        if s.count(old) != 1:
            raise SystemExit('Expected exactly one matching source block; no changes written.')
        s = s.replace(old,new,1)
    compile(s,'<patched>','exec')
    return s.replace('\n',nl).encode('utf-8')
a2=edit(a,[('''    A live qualification receipt deliberately contains a fresh qualification ID,
    so its receipt SHA changes on every successful requalification.  That is not
    an image change.  Preserve the exact digest/source/platform/rootfs binding and
    CAS-refresh only the qualification receipt when those immutable fields match.''','''    A live qualification receipt may change without an image change. A newly
    qualified image may also replace the old binding through the host ledger's
    revision CAS and active/indeterminate lease checks.'''),('''    if any(pin.get(name) != value for name, value in immutable.items()):
        raise CapabilityUnavailableError("Existing workspace image pin differs")''','''    binding_matches = all(pin.get(name) == value for name, value in immutable.items())'''),('''    if pin.get("qualification_sha256") == qualification:
        return revision''','''    if binding_matches and pin.get("qualification_sha256") == qualification:
        return revision''')])
b2=edit(b,[('''    ledger.pin["digest"] = "e" * 64
    with pytest.raises(CapabilityUnavailableError, match="image pin differs"):
        _activate_workspace_image_pin(
            ledger,
            workspace,
            image_digest=digest,
            image_source=source,
            rootfs=rootfs,
            qualification="f" * 64,
        )''','''    next_rootfs = tmp_path / "next-rootfs"
    next_rootfs.mkdir()
    # Even an identical receipt must not hide a changed image binding.
    for expected, receipt in ((3, next_qualification), (4, "f" * 64), (4, "f" * 64)):
        assert (
            _activate_workspace_image_pin(
                ledger,
                workspace,
                image_digest="e" * 64,
                image_source="f" * 40,
                rootfs=next_rootfs,
                qualification=receipt,
            )
            == expected
        )
    assert ledger.pin["digest"] == "e" * 64
    assert ledger.pin["rootfs_path"] == str(next_rootfs)
    assert len(ledger.calls) == 4''')])
p.write_bytes(a2); t.write_bytes(b2)
print('Patched WSL image activation and regression test. No workspace data changed.')
