"""模型清单入口：`/v1/models` 与面板的多选清单。

单独一个 mixin，只因为 `service.py` 有自己的行长约定（700 行）——
这里全是"读目录、拼形状"，不参与选号与转发。
"""

from __future__ import annotations

from typing import Any, Dict, List

from . import identity


class ModelsEndpoint:
    def models(self, request: Any) -> Any:
        from .httpserver import json_response

        token = request.bearer() or request.arg("api_key")
        key = self.authenticate(token)
        realm = self.resolve_realm(request.arg("realm"), key, request.header("X-Realm"))
        self._ensure_catalog(realm)
        allowed = key.allows_model if key is not None else None
        return json_response(self.catalog.openai_list(realm, allowed))

    def catalog_view(self, realm: str = "") -> Dict[str, Any]:
        """面板用：把模型列成"可以直接多选"的清单。

        `realm` 留空时返回两个域的全部（配一把 key 本来就不该被迫先选域），
        每项都带 `realm`，选中之后写进 `key.models` 的是确定的模型 id。
        """
        realms = [identity.normalize_realm(realm)] if realm else ["cn", "global"]
        rows: List[Dict[str, Any]] = []
        for item in realms:
            self._ensure_catalog(item)
            for info in self.catalog.entries(item, chat_only=True):
                if self.catalog.hidden(info.id):
                    continue
                row: Dict[str, Any] = {"id": info.id, "name": info.name, "realm": item}
                if info.alias_of:
                    row["alias_of"] = info.alias_of
                if info.max_output:
                    row["max_output_tokens"] = info.max_output
                if info.context_length:
                    row["context_length"] = info.context_length
                if info.vision is not None:
                    row["supports_vision"] = bool(info.vision)
                rows.append(row)
        rows.sort(key=lambda entry: (entry["realm"], entry["id"]))
        counts = {item: sum(1 for entry in rows if entry["realm"] == item) for item in realms}
        return {"models": rows, "realms": counts, "total": len(rows)}

    def _ensure_catalog(self, realm: str) -> None:
        if self.catalog.names(realm):
            return
        accounts = [item for item in self.store.list_accounts(realm) if item.enabled and item.access_token]
        if not accounts:
            return
        self.catalog.fetch(accounts[0], realm)

