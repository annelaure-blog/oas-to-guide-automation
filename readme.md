# OpenAPI to Guide Automation

This repository provides a Python script that converts an OpenAPI specification (YAML or JSON) into a collection of structured markdown guides. Each endpoint is rendered into its own guide, organized by tag, and includes endpoint tables, attribute details, code samples, and response references following the supplied template.

## Prerequisites

- Python 3.9+
- Dependencies listed in `requirements.txt` (`PyYAML` for YAML parsing)

Install dependencies:

```bash
pip install -r requirements.txt
```

## Usage

Run the generator by pointing it to your OpenAPI specification:

```bash
python generate_guides.py path/to/openapi.yaml --output guides
```

- `spec`: Path to a JSON or YAML OpenAPI specification.
- `--output`: Directory where the guides will be written (default: `guides/`). Guides are grouped into subfolders by the first tag on each operation and named with the HTTP verb and path.

The generated markdown follows this structure for each operation:

- Introduction with a concise description.
- Endpoint overview table with HTTP verb, path, and description.
- Attributes table aggregating request and response fields (showing accepted values, writeable/nullable/required flags).
- Endpoint-specific section with badge, URL, and code samples in cURL, Python, Java, and PHP.
- Sample response payload derived from the response schema when available.
- Responses table summarizing status codes and descriptions.

## Development

To view CLI help:

```bash
python generate_guides.py --help
```

Feel free to extend the generator to support additional media types, richer sample generation, or custom templates.
