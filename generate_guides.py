"""CLI tool to generate markdown guides from an OpenAPI specification.

The tool reads a JSON or YAML OpenAPI document and produces one markdown
file per endpoint grouped by tag. It follows the layout requested in the
prompt, including introduction, endpoint overview, attributes, code
samples, and response documentation.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Set

yaml_spec = importlib.util.find_spec("yaml")
yaml = importlib.import_module("yaml") if yaml_spec else None  # type: ignore

SUPPORTED_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}

DEFAULT_MACROS: Mapping[str, str] = {
    "pageIndex": "0",
    "pageSize": "25",
    "offset": "0",
    "limit": "500",
    "retailerId": "12345",
    "accountId": "368471940340928512",
    "parentAccountId": "425730879617900544",
    "campaignId": "544937665113018368",
    "lineItem": "6854840188706902009",
    "lineItemId": "6854840188706902009",
}


@dataclass
class FieldInfo:
    attribute: str
    data_type: str
    description: str
    accepted_values: str
    writeable: bool
    nullable: bool
    required: bool

    def render_description(self) -> str:
        availability = "Writeable? {w} / Nullable? {n} Required {r}".format(
            w="Y" if self.writeable else "N",
            n="Y" if self.nullable else "N",
            r="Y" if self.required else "N",
        )
        parts = [
            self.description.strip() or "No description provided.",
            f"Accepted values: {self.accepted_values or 'Any'}",
            availability,
        ]
        paragraphs = "".join(f"<p>{line}</p>" for line in parts)
        return f"<div>{paragraphs}</div>"


@dataclass
class OperationContext:
    method: str
    path: str
    tag: str
    summary: str
    description: str
    operation: Mapping[str, Any]
    base_url: str
    attributes: List[FieldInfo] = field(default_factory=list)
    normalized_path: str = ""

    @property
    def endpoint_title(self) -> str:
        return self.description or self.summary or f"{self.method.upper()} {self.path}"

    @property
    def filename(self) -> str:
        safe_path = self.path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
        safe_path = safe_path or "root"
        return f"{self.method.upper()}_{safe_path}.md"

    @property
    def endpoint_url(self) -> str:
        display_path = self.normalized_path or self.path
        return f"{self.base_url.rstrip('/')}{display_path}"


def load_spec(spec_path: Path) -> Mapping[str, Any]:
    if not spec_path.exists():
        raise FileNotFoundError(f"Spec file not found: {spec_path}")

    content = spec_path.read_text()
    if spec_path.suffix.lower() in {".yml", ".yaml"}:
        if yaml is None:
            raise RuntimeError("PyYAML is required to read YAML specifications")
        return yaml.safe_load(content)
    return json.loads(content)


def determine_base_url(spec: Mapping[str, Any]) -> str:
    servers = spec.get("servers") or []
    if servers and isinstance(servers, list):
        url = servers[0].get("url")
        if isinstance(url, str) and url:
            return url
    return "https://api.example.com"


def iter_operations(spec: Mapping[str, Any], base_url: str) -> Iterable[OperationContext]:
    paths: Mapping[str, Any] = spec.get("paths", {}) or {}
    collected: List[OperationContext] = []

    for path, path_item in paths.items():
        for method, operation in path_item.items():
            if method.lower() not in SUPPORTED_METHODS:
                continue
            if not isinstance(operation, Mapping):
                continue
            tags = operation.get("tags") or []
            tag = tags[0] if tags else "untagged"
            summary = operation.get("summary") or ""
            description = operation.get("description") or summary or ""
            collected.append(
                OperationContext(
                    method=method,
                    path=path,
                    tag=tag,
                    summary=summary,
                    description=description,
                    operation=operation,
                    base_url=base_url,
                    normalized_path=normalize_id_placeholders(path),
                )
            )

    for context in sorted(collected, key=lambda ctx: (ctx.tag, ctx.path, ctx.method)):
        yield context


def extract_fields(operation: Mapping[str, Any]) -> List[FieldInfo]:
    fields: Dict[str, FieldInfo] = {}

    request_body = operation.get("requestBody") or {}
    request_fields = extract_fields_from_request(request_body)
    merge_fields(fields, request_fields)

    responses = operation.get("responses") or {}
    response_fields = extract_fields_from_responses(responses)
    merge_fields(fields, response_fields)

    return sorted(fields.values(), key=lambda f: f.attribute)


def merge_fields(current: MutableMapping[str, FieldInfo], new_fields: Iterable[FieldInfo]) -> None:
    for field in new_fields:
        if field.attribute in current:
            existing = current[field.attribute]
            current[field.attribute] = FieldInfo(
                attribute=field.attribute,
                data_type=field.data_type or existing.data_type,
                description=field.description or existing.description,
                accepted_values=field.accepted_values or existing.accepted_values,
                writeable=existing.writeable or field.writeable,
                nullable=existing.nullable or field.nullable,
                required=existing.required or field.required,
            )
        else:
            current[field.attribute] = field


def extract_fields_from_request(request_body: Mapping[str, Any]) -> List[FieldInfo]:
    content = request_body.get("content") or {}
    schema = _choose_schema(content)
    if not schema:
        return []
    return list(_walk_schema(schema, writeable=True))


def extract_fields_from_responses(responses: Mapping[str, Any]) -> List[FieldInfo]:
    for preferred_code in ("200", "201", "202", "2XX"):
        if preferred_code in responses:
            schema = _choose_schema(responses[preferred_code].get("content") or {})
            if schema:
                return list(_walk_schema(schema, writeable=False))
    for response in responses.values():
        schema = _choose_schema((response or {}).get("content") or {})
        if schema:
            return list(_walk_schema(schema, writeable=False))
    return []


def _choose_schema(content: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    if not isinstance(content, Mapping):
        return None
    for media_type in (
        "application/json",
        "application/problem+json",
        "application/vnd.api+json",
        "application/x-www-form-urlencoded",
    ):
        if media_type in content:
            return content[media_type].get("schema")
    if content:
        first = next(iter(content.values()))
        if isinstance(first, Mapping):
            return first.get("schema")
    return None


def _walk_schema(
    schema: Mapping[str, Any],
    writeable: bool,
    required: bool = False,
    parent: str = "",
) -> Iterable[FieldInfo]:
    data_type = schema.get("type") or "object"
    nullable = bool(schema.get("nullable"))
    description = schema.get("description") or ""
    accepted = ""
    if "enum" in schema and isinstance(schema["enum"], Sequence):
        accepted = ", ".join(map(str, schema["enum"]))
    if "const" in schema:
        accepted = str(schema["const"])

    if data_type == "object" and "properties" in schema:
        properties: Mapping[str, Any] = schema.get("properties", {}) or {}
        required_props = set(schema.get("required", []))
        if description:
            yield FieldInfo(
                attribute=f"`{parent or 'object'}`",
                data_type=f"`{data_type}`",
                description=description,
                accepted_values=accepted,
                writeable=writeable,
                nullable=nullable,
                required=required,
            )
        for prop_name, prop_schema in properties.items():
            full_name = f"{parent}.{prop_name}" if parent else prop_name
            prop_required = prop_name in required_props
            if isinstance(prop_schema, Mapping):
                prop_type = prop_schema.get("type") or ("properties" in prop_schema and "object")
                if prop_type in {"object", "array"} or "properties" in prop_schema:
                    yield from _walk_schema(
                        prop_schema,
                        writeable=writeable,
                        required=prop_required,
                        parent=full_name,
                    )
                else:
                    yield _build_field_info(
                        full_name,
                        prop_schema,
                        writeable,
                        nullable=bool(prop_schema.get("nullable")),
                        required=prop_required,
                    )
            else:
                yield FieldInfo(
                    attribute=f"`{full_name}`",
                    data_type="`unknown`",
                    description="No description provided.",
                    accepted_values="",
                    writeable=writeable,
                    nullable=nullable,
                    required=prop_required,
                )
    elif data_type == "array" and "items" in schema:
        item_schema = schema.get("items", {})
        array_name = f"{parent}[]" if parent else "items"
        yield _build_field_info(
            array_name,
            schema,
            writeable,
            nullable=nullable,
            required=required,
        )
        if isinstance(item_schema, Mapping):
            yield from _walk_schema(item_schema, writeable, required=required, parent=array_name)
    else:
        yield _build_field_info(parent or "value", schema, writeable, nullable=nullable, required=required)


def _build_field_info(
    name: str,
    schema: Mapping[str, Any],
    writeable: bool,
    nullable: bool,
    required: bool,
) -> FieldInfo:
    data_type = schema.get("type") or "object"
    description = schema.get("description") or "No description provided."
    accepted = ""
    if "enum" in schema and isinstance(schema["enum"], Sequence):
        accepted = ", ".join(map(str, schema["enum"]))
    if "const" in schema:
        accepted = str(schema["const"])
    return FieldInfo(
        attribute=f"`{name}`",
        data_type=f"`{data_type}`",
        description=description,
        accepted_values=accepted,
        writeable=writeable,
        nullable=nullable,
        required=required,
    )


def generate_markdown(context: OperationContext) -> str:
    fields_section = render_fields(context.attributes)
    responses_section = render_responses(context.operation.get("responses", {}))
    code_samples = render_code_samples(context)

    intro = context.description or "No description provided."
    endpoint_table = textwrap.dedent(
        f"""
        | Verb | Endpoint | Description |
        | :--- | :------- | :---------- |
        | **{context.method.upper()}** | `{context.normalized_path or context.path}` | {context.description or context.summary or 'No description provided.'} |
        """
    ).strip()

    field_definitions = textwrap.dedent(
        """
        <Callout icon="📘" theme="info">
        Fields definitions

        Writeable (Y/N): Indicates if the field can be modified in requests.
        Nullable (Y/N): Indicates if the field can accept null/empty values.
        Primary Key: A unique, immutable identifier of the entity, generated internally by Criteo. Primary keys are typically ID fields (e.g., `retailerId`, `campaignId`, `lineItemId`) and are usually required in the URL path.
        </Callout>
        """
    ).strip()

    details_section = textwrap.dedent(
        f"""
        ## {context.description or context.summary or 'Endpoint'}
        {context.description or context.summary or 'No description provided.'}

        ### Method {context.method.upper()}
        `{context.endpoint_url}`

        {code_samples}
        """
    ).strip()

    sections = [
        f"# {context.description or context.summary or context.endpoint_title}",
        "## Introduction",
        intro,
        "---",
        "## Endpoint",
        endpoint_table,
        "---",
        "## Attributes",
        fields_section,
        field_definitions,
        "---",
        details_section,
        "---",
        "## Responses",
        responses_section,
    ]
    return "\n\n".join(sections).strip() + "\n"


def render_fields(fields: Sequence[FieldInfo]) -> str:
    if not fields:
        return "No attributes documented."
    header = "| Attribute | Data Type | Description |\n| :------- | :------- | :---------- |"
    rows = [f"| {f.attribute} | {f.data_type} | {f.render_description()} |" for f in fields]
    return "\n".join([header, *rows])



def render_code_samples(context: OperationContext) -> str:
    method = context.method.upper()
    url = format_sample_url(context.endpoint_url)
    has_body = method not in {"GET", "DELETE"}
    payload = build_sample_payload(context.operation)
    payload_data = apply_macro_defaults_to_payload(payload) if payload is not None else ({} if has_body else None)
    payload_json = json.dumps(payload_data, indent=4) if payload_data is not None else ""
    payload_compact = json.dumps(payload_data) if payload_data is not None else ""

    curl_lines = [
        "```curl",
        f"curl -L -X {method} '{url}' \\",
        "    -H 'Authorization: Bearer <TOKEN>' \\",
    ]
    if has_body:
        curl_lines.append("    -H 'Content-Type: application/json' \\")
    curl_lines.append("    -H 'Accept: application/json'")
    if has_body and payload_json:
        curl_lines.append(f"    -d '{payload_json}'")
    curl_lines.append("```")
    curl = "\n".join(curl_lines)

    header_lines = ["        'Authorization': 'Bearer <TOKEN>'"]
    if has_body:
        header_lines.append("        'Content-Type': 'application/json'")
    header_lines.append("        'Accept': 'application/json'")
    headers_block = ",\n".join(header_lines)

    python_lines = [
        "```python",
        "import http.client",
        "import json",
        "",
        f"conn = http.client.HTTPSConnection('{_hostname_from_url(url)}')",
    ]
    if has_body:
        python_lines.append(f"payload = json.dumps({payload_compact})")
    else:
        python_lines.append("payload = None")
    python_lines.extend(
        [
            "",
            "headers = {",
            headers_block,
            "}",
            "",
            f"conn.request('{method}', '{_path_from_url(url)}', payload if payload else None, headers)",
            "res = conn.getresponse()",
            "data = res.read()",
            "print(data.decode('utf-8'))",
            "```",
        ]
    )
    python_sample = "\n".join(python_lines)

    java_headers = [
        "            .addHeader(\"Authorization\", \"Bearer <TOKEN>\")",
    ]
    if has_body:
        java_headers.append("            .addHeader(\"Content-Type\", \"application/json\")")
    java_headers.append("            .addHeader(\"Accept\", \"application/json\")")
    java_body_line = (
        f"RequestBody body = RequestBody.create(mediaType, \"{payload_compact.replace('"', '\\\"')}\");"
        if has_body
        else "RequestBody body = null;"
    )
    java_lines = [
        "```java",
        "OkHttpClient client = new OkHttpClient().newBuilder().build();",
        "MediaType mediaType = MediaType.parse(\"application/json\");",
        java_body_line,
        "Request request = new Request.Builder()",
        f"    .url(\"{url}\")",
        f"    .method(\"{method}\", {"body" if has_body else "null"})",
        *java_headers,
        "    .build();",
        "Response response = client.newCall(request).execute();",
        "```",
    ]
    java_sample = "\n".join(java_lines)

    php_headers = [
        "      'Authorization' => 'Bearer <TOKEN>'",
    ]
    if has_body:
        php_headers.append("      'Content-Type' => 'application/json'")
    php_headers.append("      'Accept' => 'application/json'")

    php_body_line = f"    $request->setBody('{payload_compact.replace("'", "\\'{}")}');" if has_body else ""

    php_lines = [
        "```php",
        "<?php",
        "require_once 'HTTP/Request2.php';",
        "$request = new HTTP_Request2();",
        f"$request->setUrl('{url}');",
        f"$request->setMethod(HTTP_Request2::METHOD_{method});",
        "$request->setConfig(array(\"follow_redirects\" => TRUE));",
        "$request->setHeader(array(",
        *php_headers,
        "));",
        php_body_line,
        "try {",
        "    $response = $request->send();",
        "    echo $response->getBody();",
        "}",
        "catch(HTTP_Request2_Exception $e) {",
        "    echo 'Error: ' . $e->getMessage();",
        "}",
        "?>",
        "```",
    ]
    php_sample = "\n".join([line for line in php_lines if line.strip()])

    response_example = render_response_example(context.operation.get("responses", {}))

    tab_block = textwrap.dedent(
        f"""
        <Tabs>
        <TabItem value="curl" label="cURL">
        {curl.strip()}
        </TabItem>
        <TabItem value="python" label="Python">
        {python_sample.strip()}
        </TabItem>
        <TabItem value="java" label="Java">
        {java_sample.strip()}
        </TabItem>
        <TabItem value="php" label="PHP">
        {php_sample.strip()}
        </TabItem>
        </Tabs>
        """
    ).strip()

    return "\n\n".join([
        "**Sample Request**:",
        tab_block,
        "**Sample Response**",
        response_example,
    ])
def render_response_example(responses: Mapping[str, Any]) -> str:
    schema = None
    for preferred_code in ("200", "201", "202", "2XX"):
        if preferred_code in responses:
            schema = _choose_schema(responses[preferred_code].get("content") or {})
            break
    if not schema:
        for response in responses.values():
            schema = _choose_schema((response or {}).get("content") or {})
            if schema:
                break
    example_payload = build_example_from_schema(schema) if schema else {"message": "No response schema documented."}
    return f"```json\n{json.dumps(example_payload, indent=4)}\n```"


def build_sample_payload(operation: Mapping[str, Any]) -> Optional[Any]:
    request_body = operation.get("requestBody") or {}
    schema = _choose_schema(request_body.get("content") or {})
    return build_example_from_schema(schema) if schema else None


def build_example_from_schema(schema: Optional[Mapping[str, Any]]) -> Any:
    if not schema:
        return {}
    if "example" in schema:
        return schema["example"]
    schema_type = schema.get("type")
    if schema_type == "object" or ("properties" in schema and not schema_type):
        example: Dict[str, Any] = {}
        for prop_name, prop_schema in (schema.get("properties") or {}).items():
            example[prop_name] = build_example_from_schema(prop_schema)
        return example
    if schema_type == "array":
        return [build_example_from_schema(schema.get("items"))]
    if schema_type == "integer":
        return 0
    if schema_type == "number":
        return 0.0
    if schema_type == "boolean":
        return True
    if schema_type == "string":
        if "enum" in schema and schema["enum"]:
            return schema["enum"][0]
        if schema.get("format") == "date-time":
            return "2025-01-01T00:00:00Z"
        if schema.get("format") == "date":
            return "2025-01-01"
        return "string"
    if isinstance(schema.get("default"), (str, int, float, bool, dict, list)):
        return schema["default"]
    return "value"


def render_responses(responses: Mapping[str, Any]) -> str:
    if not responses:
        return "No responses documented."
    header = "| Status | Type | Message | Description |\n| :----- | :--- | :----- | :---------- |"
    rows = []
    for status in sorted(responses.keys(), key=str):
        response = responses[status]
        emoji = "🟢" if str(status).startswith(("2", "3")) else "🔴"
        description = response.get("description") if isinstance(response, Mapping) else ""
        rows.append(
            f"| {emoji} {status} | `{_response_type(response)}` | {description or '—'} | {description or 'No description provided.'} |"
        )
    return "\n".join([header, *rows])


def _response_type(response: Any) -> str:
    if isinstance(response, Mapping):
        content = response.get("content") or {}
        if content:
            return next(iter(content.keys()))
    return "response"


def _hostname_from_url(url: str) -> str:
    if "//" in url:
        return url.split("//", 1)[1].split("/", 1)[0]
    return url.split("/", 1)[0]


def _path_from_url(url: str) -> str:
    if "//" in url:
        parts = url.split("//", 1)[1]
        if "/" in parts:
            return "/" + parts.split("/", 1)[1]
        return "/"
    return url


def format_sample_url(url: str) -> str:
    formatted = url
    for macro, value in DEFAULT_MACROS.items():
        formatted = formatted.replace(f"{{{macro}}}", str(value))
    return formatted


def normalize_id_placeholders(path: str) -> str:
    pattern = re.compile(r"\{([^}]+)\}")

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if "id" in name.lower():
            kebab = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name).replace("_", "-").lower()
            return f":{kebab}"
        return match.group(0)

    return pattern.sub(_replace, path)


def apply_macro_defaults_to_payload(payload: Any) -> Any:
    if isinstance(payload, Mapping):
        return {k: apply_macro_defaults_to_payload(v) for k, v in payload.items()}
    if isinstance(payload, list):
        return [apply_macro_defaults_to_payload(item) for item in payload]
    if isinstance(payload, str):
        return _replace_macros_in_string(payload)
    return payload


def _replace_macros_in_string(value: str) -> str:
    updated = value
    for macro, default in DEFAULT_MACROS.items():
        updated = updated.replace(f"{{{macro}}}", str(default))
    return updated


def generate_guides(spec_path: Path, output_dir: Path) -> None:
    spec = load_spec(spec_path)
    base_url = determine_base_url(spec)
    output_dir.mkdir(parents=True, exist_ok=True)

    for context in iter_operations(spec, base_url):
        context.attributes = extract_fields(context.operation)
        tag_dir = output_dir / context.tag
        tag_dir.mkdir(parents=True, exist_ok=True)
        guide_path = tag_dir / context.filename
        guide_markdown = generate_markdown(context)
        guide_path.write_text(guide_markdown)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate markdown guides from an OpenAPI specification.")
    parser.add_argument("spec", type=Path, help="Path to the OpenAPI specification (YAML or JSON).")
    parser.add_argument("--output", type=Path, default=Path("guides"), help="Output directory for generated guides.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    generate_guides(args.spec, args.output)


if __name__ == "__main__":
    main()
