import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { PushToggle } from "../components/PushToggle";

describe("PushToggle", () => {
  it("does not claim to be subscribed when Push is missing", () => {
    render(<PushToggle />);
    expect(screen.queryByText(/^subscribed$/i)).toBeNull();
    expect(screen.getByText(/ping this device only when a sweep actually stops/i)).toBeInTheDocument();
  });
});
