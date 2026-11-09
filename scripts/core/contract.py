"""HTTP 响应契约校验。

首版刻意只校验响应：内置 JSON Schema 子集，以及本地 OpenAPI 3 JSON 文件。
契约引用错误属于规格错误；真实响应不符合契约才属于测试失败。
"""

import json
import os
import re


class ContractSpecError(Exception):
    """契约配置或引用无效（不是被测系统行为失败）。"""


def _pointer(doc, ref):
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise ContractSpecError("只支持当前文档内的 $ref（如 #/components/schemas/User），实际是 %r" % ref)
    cur = doc
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(cur, dict) or part not in cur:
            raise ContractSpecError("$ref 找不到：%s" % ref)
        cur = cur[part]
    return cur


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise ContractSpecError("契约文件不存在：%s" % path)
    except json.JSONDecodeError as e:
        raise ContractSpecError("契约 JSON 无法解析：%s（第 %d 行第 %d 列）" % (path, e.lineno, e.colno))


def build_registry(contract, base_dir=None):
    """加载规格顶层 contract，返回可复用 registry。没有 contract 时仍返回空 registry。"""
    contract = contract or {}
    if not isinstance(contract, dict):
        raise ContractSpecError("顶层 contract 必须是对象")
    schemas = contract.get("schemas") or {}
    sources = contract.get("sources") or {}
    if not isinstance(schemas, dict) or not isinstance(sources, dict):
        raise ContractSpecError("contract.schemas 与 contract.sources 必须是对象")
    loaded = {}
    for name, cfg in sources.items():
        if not isinstance(cfg, dict) or cfg.get("type") != "openapi3" or not cfg.get("file"):
            raise ContractSpecError("contract.sources.%s 必须包含 type=openapi3 与 file" % name)
        path = cfg["file"]
        if not os.path.isabs(path):
            path = os.path.normpath(os.path.join(base_dir or "", path))
        doc = _load_json(path)
        if not isinstance(doc, dict) or not str(doc.get("openapi", "")).startswith("3."):
            raise ContractSpecError("契约来源 %s 不是 OpenAPI 3 JSON 文档" % name)
        operations = {}
        for _, path_item in (doc.get("paths") or {}).items():
            if not isinstance(path_item, dict):
                continue
            for method, operation in path_item.items():
                if method.lower() not in ("get", "post", "put", "patch", "delete", "head", "options") or not isinstance(operation, dict):
                    continue
                op_id = operation.get("operationId")
                if not op_id:
                    continue
                if op_id in operations:
                    raise ContractSpecError("契约来源 %s 的 operationId 重复：%s" % (name, op_id))
                operations[op_id] = operation
        loaded[name] = {"doc": doc, "operations": operations, "path": path}
    return {"schemas": schemas, "sources": loaded, "max_errors": int((contract.get("options") or {}).get("max_errors", 20))}


def _response_rule(assertion, registry):
    """将内置 schema / OpenAPI response 统一为 schema、状态码、content-type。"""
    if "schema" in assertion:
        name = assertion["schema"]
        if name not in registry["schemas"]:
            raise ContractSpecError("内置响应契约 schema 不存在：%s" % name)
        return {"source": "schema", "schema_name": name, "schema": registry["schemas"][name],
                "status": assertion.get("状态码"), "content_type": assertion.get("内容类型")}
    source = assertion.get("来源")
    op_id = assertion.get("operationId")
    response_key = str(assertion.get("响应", ""))
    if source not in registry["sources"]:
        raise ContractSpecError("响应契约来源不存在：%s" % source)
    operation = registry["sources"][source]["operations"].get(op_id)
    if operation is None:
        raise ContractSpecError("OpenAPI 来源 %s 中不存在 operationId=%s" % (source, op_id))
    response = (operation.get("responses") or {}).get(response_key)
    if response is None:
        raise ContractSpecError("operationId=%s 未定义响应 %s" % (op_id, response_key))
    if "$ref" in response:
        response = _pointer(registry["sources"][source]["doc"], response["$ref"])
    content = response.get("content") or {}
    content_type = assertion.get("内容类型") or (next(iter(content)) if content else None)
    media = content.get(content_type) if content_type else None
    schema = (media or {}).get("schema")
    if schema and "$ref" in schema:
        schema = _pointer(registry["sources"][source]["doc"], schema["$ref"])
    return {"source": source, "operation_id": op_id, "response_key": response_key,
            "schema": schema, "status": assertion.get("状态码", response_key if response_key.isdigit() else None),
            "content_type": content_type, "doc": registry["sources"][source]["doc"]}


def validate_assertion(assertion, registry):
    """静态校验引用是否可解析。"""
    _response_rule(assertion, registry)


def _type_ok(value, expected):
    if expected == "object": return isinstance(value, dict)
    if expected == "array": return isinstance(value, list)
    if expected == "string": return isinstance(value, str)
    if expected == "integer": return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number": return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean": return isinstance(value, bool)
    if expected == "null": return value is None
    return True


def _validate(value, schema, root, path="", errors=None, seen=None):
    errors = errors if errors is not None else []
    seen = seen if seen is not None else set()
    if not isinstance(schema, dict):
        return errors
    if "$ref" in schema:
        ref = schema["$ref"]
        if ref in seen:
            raise ContractSpecError("检测到循环 $ref：%s" % ref)
        return _validate(value, _pointer(root, ref), root, path, errors, seen | {ref})
    if schema.get("nullable") and value is None:
        return errors
    typ = schema.get("type")
    if typ and not _type_ok(value, typ):
        errors.append({"code": "type", "instance_path": path or "/", "message": "实际 %s，期望 %s" % (type(value).__name__, typ)})
        return errors
    if "const" in schema and value != schema["const"]:
        errors.append({"code": "const", "instance_path": path or "/", "message": "实际值不等于 const"})
    if "enum" in schema and value not in schema["enum"]:
        errors.append({"code": "enum", "instance_path": path or "/", "message": "实际值不属于 enum"})
    if isinstance(value, dict):
        required = schema.get("required") or []
        for key in required:
            if key not in value:
                errors.append({"code": "required", "instance_path": (path or "/") + ("" if path.endswith("/") else "/") + key, "message": "缺少必填字段 %s" % key})
        props = schema.get("properties") or {}
        for key, child in props.items():
            if key in value:
                _validate(value[key], child, root, (path + "/" + key), errors, seen)
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    errors.append({"code": "additionalProperties", "instance_path": path or "/", "message": "不允许额外字段 %s" % key})
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append({"code": "minItems", "instance_path": path or "/", "message": "数组长度 %d 小于 %d" % (len(value), schema["minItems"])})
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append({"code": "maxItems", "instance_path": path or "/", "message": "数组长度 %d 大于 %d" % (len(value), schema["maxItems"])})
        if "items" in schema:
            for i, item in enumerate(value): _validate(item, schema["items"], root, path + "/" + str(i), errors, seen)
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]: errors.append({"code":"minLength","instance_path":path or "/","message":"字符串长度不足"})
        if "maxLength" in schema and len(value) > schema["maxLength"]: errors.append({"code":"maxLength","instance_path":path or "/","message":"字符串长度超限"})
        if "pattern" in schema and re.search(schema["pattern"], value) is None: errors.append({"code":"pattern","instance_path":path or "/","message":"字符串不匹配 pattern"})
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]: errors.append({"code":"minimum","instance_path":path or "/","message":"数值小于 minimum"})
        if "maximum" in schema and value > schema["maximum"]: errors.append({"code":"maximum","instance_path":path or "/","message":"数值大于 maximum"})
    return errors


def _header(headers, name):
    for key, value in (headers or {}).items():
        if key.lower() == name.lower(): return value
    return ""


def validate_response(assertion, resp, registry):
    """返回 (passed, detail, evidence)。"""
    rule = _response_rule(assertion, registry)
    evidence = {k: v for k, v in rule.items() if k in ("source", "schema_name", "operation_id", "response_key", "status", "content_type")}
    evidence["actual_status"] = resp.get("status")
    actual_ct = _header(resp.get("headers"), "content-type")
    evidence["actual_content_type"] = actual_ct
    violations = []
    wanted_status = rule.get("status")
    if wanted_status is not None and str(resp.get("status")) != str(wanted_status):
        violations.append({"code":"status", "instance_path":"/", "message":"HTTP %s，期望 %s" % (resp.get("status"), wanted_status)})
    wanted_ct = rule.get("content_type")
    if wanted_ct and actual_ct.split(";", 1)[0].strip().lower() != wanted_ct.lower():
        violations.append({"code":"content-type", "instance_path":"/", "message":"Content-Type=%s，期望 %s" % (actual_ct or "(缺失)", wanted_ct)})
    if rule.get("schema") is not None:
        if resp.get("json") is None:
            violations.append({"code":"json", "instance_path":"/", "message":"响应不是可解析 JSON，无法校验结构"})
        else:
            root = rule.get("doc") or {"components": {"schemas": registry["schemas"]}}
            violations.extend(_validate(resp["json"], rule["schema"], root))
    max_errors = registry.get("max_errors", 20)
    evidence["violations"] = violations[:max_errors]
    if not violations:
        return True, "响应契约符合（%s）" % (rule.get("operation_id") or rule.get("schema_name")), evidence
    text = "；".join(v["instance_path"] + " " + v["message"] for v in evidence["violations"])
    return False, "契约不符：%s" % text, evidence
