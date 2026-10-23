// Protocol fixtures (protocol/fixtures/*.json) as typed test inputs. They are
// schema-validated by src/protocol/fixtures.test.ts, so tests built on them use
// the same data the daemon and hub are tested with.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import type { Board, BoardsResponse, Devbox, UiEvent } from "../protocol/types";

const FIXTURES = resolve(__dirname, "../../../protocol/fixtures");

export function fixture<T = UiEvent>(name: string): T {
  return JSON.parse(readFileSync(resolve(FIXTURES, `${name}.json`), "utf8")) as T;
}

const withoutType = ({ type: _type, ...rest }: { type: string } & Devbox): Devbox => rest;

/**
 * A `GET /api/boards` answer built from the fixtures: the three boards of
 * dev-daniel's full snapshot (two github, one forge=none) plus dev-acme's
 * stale-looking box that the hub refused.
 */
export function boardsResponse(): BoardsResponse {
  const snapshot = fixture<{ boards: Board[] }>("snapshot-full-three-boards");
  return {
    boards: snapshot.boards,
    devboxes: [
      withoutType(fixture("devbox-status-online")),
      withoutType(fixture("devbox-status-refused")),
    ],
  };
}
