import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { createStore } from "../store/store";
import { boardsResponse } from "../test/fixtures";
import { Controls } from "./Controls";
import { StoreProvider } from "./StoreContext";

function setup(stale = false) {
  const store = createStore();
  const data = boardsResponse();
  const archived = { ...data.boards[0], hub_meta: { archived: true }, stale };
  store.load({ ...data, boards: [archived, ...data.boards.slice(1)] });
  const onArchive = vi.fn();
  render(
    <StoreProvider store={store}>
      <Controls onArchive={onArchive} />
    </StoreProvider>,
  );
  return { archived, onArchive };
}

describe("Controls archived boards", () => {
  it("lists archived boards under Archived (n) and unticking asks to restore", () => {
    const { archived, onArchive } = setup();
    expect(screen.getByText("Archived (1)")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox"));
    expect(onArchive).toHaveBeenCalledWith(archived.key, false);
  });

  it("archives the selected board", () => {
    const { onArchive } = setup();
    const select = screen.getByLabelText("Board");
    const key = (select.querySelectorAll("option")[1] as HTMLOptionElement)
      .value;
    fireEvent.change(select, { target: { value: key } });
    fireEvent.click(screen.getByRole("button", { name: "Archive" }));
    expect(onArchive).toHaveBeenCalledWith(key, true);
  });

  it("disables Archive and restore for boards on an offline box", () => {
    const store = createStore();
    const data = boardsResponse();
    const [first, second] = data.boards;
    store.load({
      ...data,
      boards: [
        { ...first, hub_meta: { archived: true }, stale: true },
        { ...second, stale: true },
      ],
    });
    render(
      <StoreProvider store={store}>
        <Controls onArchive={vi.fn()} />
      </StoreProvider>,
    );
    expect(screen.getByRole("checkbox")).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Board"), {
      target: { value: second.key },
    });
    expect(screen.getByRole("button", { name: "Archive" })).toBeDisabled();
  });
});
