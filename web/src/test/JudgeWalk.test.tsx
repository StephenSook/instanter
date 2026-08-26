import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { JudgeWalk } from "../components/JudgeWalk";
import { APK_URL, TESTFLIGHT_URL } from "../data";

describe("JudgeWalk", () => {
  it("is a numbered itinerary with live deep links, not a stored script", () => {
    render(<JudgeWalk />);
    expect(screen.getByRole("heading", { name: /walk the door/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /the number/i })).toHaveAttribute("href", "/#live-proof");
    expect(screen.getByRole("link", { name: /sweep the queue/i })).toHaveAttribute("href", "/#sweep");
    expect(screen.getByRole("link", { name: /open a packet/i })).toHaveAttribute(
      "href",
      "/#/case/26ED00101",
    );
    expect(screen.getByRole("link", { name: /ios, on a phone/i })).toHaveAttribute(
      "href",
      TESTFLIGHT_URL,
    );
    expect(screen.getByRole("link", { name: /android apk/i })).toHaveAttribute("href", APK_URL);
    expect(screen.getByRole("link", { name: /count the rows yourself/i })).toHaveAttribute(
      "href",
      "/api/stats",
    );
    expect(screen.getByRole("link", { name: /photograph a summons/i })).toHaveAttribute(
      "href",
      "/#ocr",
    );
    expect(screen.getByRole("link", { name: /the statute on paper/i })).toHaveAttribute(
      "href",
      "/evidence",
    );
  });
});
