import "@testing-library/jest-dom/vitest";

// jsdom implements neither of these, and both are load-bearing here: the
// console disables motion under a reduced-motion preference, and every panel
// reads from the network. Leaving them undefined would make components throw
// for reasons that have nothing to do with what is being tested.
if (!window.ResizeObserver) {
  window.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}
