"""Exercise real dbus-next decorators without a bus or any secret backend."""

from potbot.syn_release import secret_service_proxy as proxy


def test_runtime_decorators_preserve_secret_service_wire_signatures():
    session, item, collection, service = proxy._build_interfaces()
    state = proxy.ProxyState()
    record = proxy.ItemRecord("fixture-service", "fixture-slot", {}, "fixture", 1, 1)
    objects = [
        session(state, "/fixture"),
        item(state, "/fixture", record),
        collection(state),
        service(state, collection(state)),
    ]
    signatures = [
        {
            method.name: (method.in_signature, method.out_signature)
            for method in obj.introspect().methods
        }
        for obj in objects
    ]
    assert signatures[0] == {"Close": ("", "")}
    assert signatures[1] == {
        "Delete": ("", "o"),
        "GetSecret": ("o", "(oayays)"),
        "SetSecret": ("(oayays)", ""),
    }
    assert signatures[2]["CreateItem"] == ("a{sv}(oayays)b", "oo")
    assert signatures[3]["OpenSession"] == ("sv", "vo")
    assert signatures[3]["GetSecrets"] == ("aoo", "a{o(oayays)}")
    assert signatures[3]["Unlock"] == ("ao", "aoo")
    assert state.items == {} and state.sessions == set()
