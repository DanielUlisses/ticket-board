import { readFileSync } from "node:fs";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Markdown } from "./Markdown";

const TABLE = "| NN | Cmd |\n|---|---|\n| 35 | install.sh daemon\\|hub |\n| 36 | `x` |";

describe("Markdown GFM", () => {
  it("renders a table with an escaped pipe inside its cell", () => {
    const { container } = render(<Markdown source={TABLE} />);
    const rows = container.querySelectorAll("tr");
    expect(rows).toHaveLength(3);
    const cells = rows[1].querySelectorAll("td");
    expect(cells).toHaveLength(2);
    expect(cells[1].textContent).toBe("install.sh daemon|hub");
    expect(container.querySelector("table")?.parentElement?.className).toMatch(/tableWrap/);
  });

  it("renders strikethrough as del", () => {
    const { container } = render(<Markdown source="~~x~~" />);
    expect(container.querySelector("del")?.textContent).toBe("x");
  });

  it("renders task lists as disabled checkboxes", () => {
    render(<Markdown source="- [ ] a" />);
    const box = screen.getByRole("checkbox");
    expect(box).toBeDisabled();
  });

  it("renders autolinks", () => {
    render(<Markdown source="see https://example.com now" />);
    expect(screen.getByRole("link").getAttribute("href")).toBe("https://example.com");
  });

  it("drops raw HTML in a cell so script/onerror stay inert", () => {
    const { container } = render(
      <Markdown source={"| a |\n|---|\n| hello <script>alert(1)</script><img src=x onerror=alert(1)> |"} />,
    );
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("[onerror]")).toBeNull();
    expect(container.querySelector("td")?.textContent).toContain("hello");
  });

  it("scrolls a wide table inside its wrapper", () => {
    const css = readFileSync(`${import.meta.dirname}/Markdown.module.css`, "utf8");
    expect(css).toMatch(/\.tableWrap\s*\{[^}]*overflow-x:\s*auto/);
    expect(css).toMatch(/\.tableWrap th,\s*\.tableWrap td\s*\{[^}]*overflow-wrap:\s*normal/);
  });
});
