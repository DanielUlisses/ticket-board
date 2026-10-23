// Everything renders through React: no component may inject raw HTML.
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, it } from "vitest";

const sources = (dir: string): string[] =>
  readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? sources(join(dir, e.name)) : /\.tsx?$/.test(e.name) ? [join(dir, e.name)] : [],
  );

it("no source writes raw HTML (React prop, DOM properties or methods)", () => {
  // Split so this file does not match itself.
  const banned = [
    "dangerously" + "SetInnerHTML",
    "inner" + "HTML",
    "outer" + "HTML",
    "insertAdjacent" + "HTML",
    "document." + "write",
  ];
  const hits = sources(__dirname).filter((f) => banned.some((b) => readFileSync(f, "utf8").includes(b)));
  expect(hits).toEqual([]);
});
