import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// React Testing Library only auto-cleans when the runner exposes a global
// afterEach; vitest does with `globals: true`, but registering it explicitly
// keeps a stray render from one test leaking into the next.
afterEach(() => cleanup());
