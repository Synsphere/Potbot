"""Secret Service façade backed entirely by the parent-node keystore authority.

The façade keeps no secret values. It implements the bounded subset used by
SecretStorage/keyring: the default collection, plain sessions, exact
service/username searches, create/replace, read, update and delete.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
from dataclasses import dataclass
from typing import Any

from potbot.syn_release.parent_keystore_client import (
    ParentKeystoreError,
    secret_delete,
    secret_get,
    secret_search,
    secret_set,
)

BUS_NAME = "org.freedesktop.secrets"
SERVICE_PATH = "/org/freedesktop/secrets"
COLLECTION_PATH = "/org/freedesktop/secrets/collection/default"
DEFAULT_ALIAS_PATH = "/org/freedesktop/secrets/aliases/default"
NO_PROMPT = "/"
SERVICE_IFACE = "org.freedesktop.Secret.Service"
COLLECTION_IFACE = "org.freedesktop.Secret.Collection"
ITEM_IFACE = "org.freedesktop.Secret.Item"
SESSION_IFACE = "org.freedesktop.Secret.Session"


def _item_id(service: str, account: str) -> str:
    return hashlib.sha256((service + "\0" + account).encode("utf-8")).hexdigest()


def _item_path(service: str, account: str) -> str:
    return COLLECTION_PATH + "/" + _item_id(service, account)


def _keyring_identity(attributes: dict[str, str]) -> tuple[str, str | None]:
    """Extract keyring's stable service/username identity from Secret Service attrs."""
    if not isinstance(attributes, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in attributes.items()
    ):
        raise ValueError("Secret Service attributes must be text pairs")
    service = attributes.get("service", "")
    account = attributes.get("username")
    if not service:
        # SecretStorage callers outside keyring commonly use xdg:schema plus
        # application-specific attributes. Do not invent an authority scope;
        # a concrete service identity is mandatory for the parent policy.
        raise ValueError("Parent-backed Secret Service requires a service attribute")
    return service, account


def _plain_secret(secret: list[Any]) -> tuple[str, bytes, str]:
    if not isinstance(secret, list) or len(secret) != 4:
        raise ValueError("Secret Service secret structure is invalid")
    session, parameters, value, content_type = secret
    if (
        not isinstance(session, str)
        or parameters not in (b"", bytearray())
        or not isinstance(value, (bytes, bytearray))
        or not isinstance(content_type, str)
    ):
        raise ValueError("Only plain Secret Service sessions are accepted")
    return session, bytes(value), content_type


@dataclass(slots=True)
class ItemRecord:
    service: str
    account: str
    attributes: dict[str, str]
    label: str
    created: int
    modified: int


class ProxyState:
    def __init__(self) -> None:
        self.bus: Any = None
        self.items: dict[str, tuple[Any, ItemRecord]] = {}
        self.sessions: set[str] = set()

    def record(
        self,
        service: str,
        account: str,
        *,
        attributes: dict[str, str] | None = None,
        label: str | None = None,
    ) -> str:
        path = _item_path(service, account)
        now = int(time.time())
        existing = self.items.get(path)
        if existing is not None:
            interface, record = existing
            record.attributes = dict(attributes or record.attributes)
            record.label = label or record.label
            record.modified = now
            return path
        attrs = dict(attributes or {"service": service, "username": account})
        record = ItemRecord(service, account, attrs, label or account, now, now)
        interface = SecretItem(self, path, record)
        self.items[path] = (interface, record)
        if self.bus is not None:
            self.bus.export(path, interface)
        return path

    def forget(self, path: str) -> None:
        if path in self.items:
            if self.bus is not None:
                self.bus.unexport(path)
            self.items.pop(path, None)


# dbus-next is intentionally imported only by the daemon. The rest of Potbot's
# source tree and CI do not gain a mandatory D-Bus dependency.
def _dbus_types():
    from dbus_next import DBusError, Variant
    from dbus_next.constants import PropertyAccess
    from dbus_next.service import ServiceInterface, dbus_property, method

    return DBusError, Variant, PropertyAccess, ServiceInterface, dbus_property, method


DBusError: Any
Variant: Any
PropertyAccess: Any
ServiceInterface: Any
# Filled by _install_types() before service classes are instantiated in main().


def _error(message: str) -> Exception:
    return DBusError("org.freedesktop.Secret.Error.IsLocked", message)


class _DeferredInterface:
    """Marker replaced with a real ServiceInterface base during module bootstrap."""


def _build_interfaces() -> tuple[type, type, type, type]:
    global DBusError, Variant, PropertyAccess, ServiceInterface
    DBusError, Variant, PropertyAccess, ServiceInterface, dbus_property, method = _dbus_types()

    class SecretSession(ServiceInterface):
        def __init__(self, state: ProxyState, path: str) -> None:
            super().__init__(SESSION_IFACE)
            self.state, self.path = state, path

        @method(name="Close")
        def close(self) -> "":
            self.state.sessions.discard(self.path)
            if self.state.bus is not None:
                self.state.bus.unexport(self.path)
            return None

    class SecretItem(ServiceInterface):
        def __init__(self, state: ProxyState, path: str, record: ItemRecord) -> None:
            super().__init__(ITEM_IFACE)
            self.state, self.path, self.record = state, path, record

        @dbus_property(access=PropertyAccess.READ, name="Locked")
        def locked(self) -> "b":
            return False

        @dbus_property(access=PropertyAccess.READWRITE, name="Label")
        def label(self) -> "s":
            return self.record.label

        @label.setter
        def label(self, value: "s") -> None:
            self.record.label = value
            self.record.modified = int(time.time())

        @dbus_property(access=PropertyAccess.READWRITE, name="Attributes")
        def attributes(self) -> "a{ss}":
            return dict(self.record.attributes)

        @attributes.setter
        def attributes(self, value: "a{ss}") -> None:
            service, account = _keyring_identity(dict(value))
            if account != self.record.account or service != self.record.service:
                raise _error("Changing a parent keystore identity is not supported")
            self.record.attributes = dict(value)
            self.record.modified = int(time.time())

        @dbus_property(access=PropertyAccess.READ, name="Created")
        def created(self) -> "t":
            return self.record.created

        @dbus_property(access=PropertyAccess.READ, name="Modified")
        def modified(self) -> "t":
            return self.record.modified

        @method(name="GetSecret")
        async def get_secret(self, session: "o") -> "(oayays)":
            if session not in self.state.sessions:
                raise _error("Unknown Secret Service session")
            try:
                value = await asyncio.to_thread(
                    secret_get, self.record.service, self.record.account
                )
            except ParentKeystoreError as error:
                raise _error(str(error)) from error
            if value is None:
                raise DBusError(
                    "org.freedesktop.DBus.Error.UnknownObject",
                    "Parent keystore item no longer exists",
                )
            return [session, b"", value.encode("utf-8"), "text/plain; charset=utf-8"]

        @method(name="SetSecret")
        async def set_secret(self, secret: "(oayays)") -> "":
            _session, raw, _content_type = _plain_secret(secret)
            try:
                value = raw.decode("utf-8")
                await asyncio.to_thread(
                    secret_set, self.record.service, self.record.account, value
                )
            except (ParentKeystoreError, UnicodeError) as error:
                raise _error(str(error)) from error
            self.record.modified = int(time.time())
            return None

        @method(name="Delete")
        async def delete(self) -> "o":
            try:
                await asyncio.to_thread(
                    secret_delete, self.record.service, self.record.account
                )
            except ParentKeystoreError as error:
                raise _error(str(error)) from error
            self.state.forget(self.path)
            return NO_PROMPT

    class SecretCollection(ServiceInterface):
        def __init__(self, state: ProxyState) -> None:
            super().__init__(COLLECTION_IFACE)
            self.state = state
            self._label = "Parent Node Keystore"
            self._created = int(time.time())

        @dbus_property(access=PropertyAccess.READ, name="Items")
        def items(self) -> "ao":
            return sorted(self.state.items)

        @dbus_property(access=PropertyAccess.READWRITE, name="Label")
        def label(self) -> "s":
            return self._label

        @label.setter
        def label(self, value: "s") -> None:
            self._label = value

        @dbus_property(access=PropertyAccess.READ, name="Locked")
        def locked(self) -> "b":
            return False

        @dbus_property(access=PropertyAccess.READ, name="Created")
        def created(self) -> "t":
            return self._created

        @dbus_property(access=PropertyAccess.READ, name="Modified")
        def modified(self) -> "t":
            return int(time.time())

        @method(name="SearchItems")
        async def search_items(self, attributes: "a{ss}") -> "ao":
            try:
                service, account = _keyring_identity(dict(attributes))
                accounts = await asyncio.to_thread(secret_search, service, account)
            except (ParentKeystoreError, ValueError) as error:
                raise _error(str(error)) from error
            return [
                self.state.record(
                    service,
                    item,
                    attributes={**dict(attributes), "service": service, "username": item},
                )
                for item in accounts
            ]

        @method(name="CreateItem")
        async def create_item(
            self, properties: "a{sv}", secret: "(oayays)", replace: "b"
        ) -> "oo":
            try:
                attrs_variant = properties.get("org.freedesktop.Secret.Item.Attributes")
                label_variant = properties.get("org.freedesktop.Secret.Item.Label")
                if attrs_variant is None or getattr(attrs_variant, "signature", "") != "a{ss}":
                    raise ValueError("Secret Service item attributes are required")
                attributes = dict(attrs_variant.value)
                service, account = _keyring_identity(attributes)
                if account is None:
                    raise ValueError("Secret Service writes require a username attribute")
                _session, raw, _content_type = _plain_secret(secret)
                value = raw.decode("utf-8")
                if not replace:
                    existing = await asyncio.to_thread(secret_get, service, account)
                    if existing is not None:
                        raise ValueError("Secret Service item already exists")
                await asyncio.to_thread(secret_set, service, account, value)
                label = (
                    str(label_variant.value)
                    if label_variant is not None and getattr(label_variant, "signature", "") == "s"
                    else account
                )
            except (ParentKeystoreError, ValueError, UnicodeError) as error:
                raise _error(str(error)) from error
            path = self.state.record(
                service, account, attributes=attributes, label=label
            )
            return [path, NO_PROMPT]

        @method(name="Delete")
        def delete(self) -> "o":
            raise _error("The parent default collection cannot be deleted")

    class SecretService(ServiceInterface):
        def __init__(self, state: ProxyState, collection: SecretCollection) -> None:
            super().__init__(SERVICE_IFACE)
            self.state, self.collection = state, collection

        @dbus_property(access=PropertyAccess.READ, name="Collections")
        def collections(self) -> "ao":
            return [COLLECTION_PATH]

        @method(name="OpenSession")
        def open_session(self, algorithm: "s", input_value: "v") -> "vo":
            if algorithm != "plain":
                raise DBusError(
                    "org.freedesktop.DBus.Error.NotSupported",
                    "Parent proxy supports Secret Service plain sessions only",
                )
            path = SERVICE_PATH + "/session/" + os.urandom(16).hex()
            session = SecretSession(self.state, path)
            self.state.sessions.add(path)
            self.state.bus.export(path, session)
            return [Variant("s", ""), path]

        @method(name="SearchItems")
        async def search_items(self, attributes: "a{ss}") -> "aoao":
            paths = await self.collection.search_items(attributes)
            return [paths, []]

        @method(name="Unlock")
        def unlock(self, objects: "ao") -> "aoo":
            return [list(objects), NO_PROMPT]

        @method(name="Lock")
        def lock(self, objects: "ao") -> "aoo":
            return [[], NO_PROMPT]

        @method(name="GetSecrets")
        async def get_secrets(self, items: "ao", session: "o") -> "a{o(oayays)}":
            if session not in self.state.sessions:
                raise _error("Unknown Secret Service session")
            results: dict[str, list[Any]] = {}
            for path in items:
                entry = self.state.items.get(path)
                if entry is None:
                    continue
                interface, _record = entry
                results[path] = await interface.get_secret(session)
            return results

        @method(name="ReadAlias")
        def read_alias(self, name: "s") -> "o":
            return COLLECTION_PATH if name in {"default", "session"} else NO_PROMPT

        @method(name="SetAlias")
        def set_alias(self, name: "s", collection: "o") -> "":
            if name != "default" or collection != COLLECTION_PATH:
                raise _error("Only the parent-backed default collection is supported")
            return None

        @method(name="CreateCollection")
        def create_collection(self, properties: "a{sv}", alias: "s") -> "oo":
            if alias not in {"", "default"}:
                raise _error("Only the parent-backed default collection is supported")
            return [COLLECTION_PATH, NO_PROMPT]

    # Expose the nested classes to ProxyState.record at runtime.
    globals()["SecretItem"] = SecretItem
    return SecretSession, SecretItem, SecretCollection, SecretService


# Runtime-filled global referenced by ProxyState.record.
SecretItem: Any = None


async def _serve() -> None:
    from dbus_next.aio import MessageBus
    from dbus_next.constants import RequestNameReply

    _SecretSession, _SecretItem, SecretCollection, SecretService = _build_interfaces()
    state = ProxyState()
    bus = await MessageBus().connect()
    state.bus = bus
    collection = SecretCollection(state)
    alias = SecretCollection(state)
    service = SecretService(state, collection)
    bus.export(SERVICE_PATH, service)
    bus.export(COLLECTION_PATH, collection)
    # SecretStorage constructs the default collection by this alias path directly.
    bus.export(DEFAULT_ALIAS_PATH, alias)
    reply = await bus.request_name(BUS_NAME)
    if reply != RequestNameReply.PRIMARY_OWNER:
        raise RuntimeError("Another Secret Service already owns the WSL session bus")
    await bus.wait_for_disconnect()


def main() -> int:
    try:
        asyncio.run(_serve())
        return 0
    except (ParentKeystoreError, RuntimeError, OSError, ImportError) as error:
        print("Parent Secret Service proxy failed: " + str(error), file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
