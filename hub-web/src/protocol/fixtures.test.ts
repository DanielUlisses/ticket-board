// The protocol fixtures are the contract between daemon, hub and web: every one
// must validate against protocol/schema.json, and every deliberately broken one
// must be rejected at the point it was broken.
import { readdirSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import Ajv2020 from "ajv/dist/2020";
import { describe, expect, it } from "vitest";

const PROTOCOL = resolve(__dirname, "../../../protocol");
const readJson = (path: string): unknown => JSON.parse(readFileSync(path, "utf8"));
const jsonFiles = (dir: string) =>
  readdirSync(join(PROTOCOL, dir))
    .filter((f) => f.endsWith(".json"))
    .sort()
    .map((f) => [f, join(PROTOCOL, dir, f)] as const);

const ajv = new Ajv2020({ allErrors: true });
ajv.addKeyword("x-protocol-version");
const validate = ajv.compile(readJson(join(PROTOCOL, "schema.json")) as object);

const valid = jsonFiles("fixtures");
const invalid = jsonFiles("fixtures-invalid");

// Where each broken fixture must fail: [JSON pointer of the instance, keyword].
// A fixture rejected for another reason is not testing what it claims to.
const EXPECTED_ERROR: Record<string, readonly [string, string]> = {
  "board-key-missing-devbox.json": ["/boards/1/key", "pattern"],
  "hello-bad-protocol-version.json": ["/protocol_version", "pattern"],
  "manual-task-bad-source.json": ["/boards/0/tickets/0/manual_tasks/0/source", "enum"],
  "message-without-type.json": ["", "required"],
  "rpc-launch-missing-manual.json": ["/params", "required"],
  "rpc-resolve-nocode-empty-reason.json": ["/params/reason", "minLength"],
  "rpc-result-missing-output-tail.json": ["", "required"],
  "rpc-unknown-method.json": ["/method", "enum"],
  "synthesis-gemini-backend.json": ["/backend", "oneOf"],
  "ticket-missing-synthesis-status.json": ["/boards/0/tickets/0", "required"],
  "ticket-unknown-column.json": ["/boards/2/tickets/0/col", "enum"],
};

describe("protocol fixtures", () => {
  it("exist in both kinds", () => {
    expect(valid.length).toBeGreaterThan(0);
    expect(invalid.length).toBeGreaterThan(0);
  });

  it.each(valid)("%s validates", (_name, path) => {
    const ok = validate(readJson(path));
    expect(validate.errors ?? [], ajv.errorsText(validate.errors)).toEqual([]);
    expect(ok).toBe(true);
  });

  it.each(invalid)("%s is rejected where it was broken", (name, path) => {
    expect(Object.keys(EXPECTED_ERROR), `add ${name} to EXPECTED_ERROR`).toContain(name);
    expect(validate(readJson(path))).toBe(false);
    // allErrors also reports a oneOf's failing branches; the intended error must be among them.
    const found = (validate.errors ?? []).map((e) => [e.instancePath, e.keyword]);
    expect(found).toContainEqual([...EXPECTED_ERROR[name]]);
  });
});
