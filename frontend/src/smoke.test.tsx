import { render, screen } from "@testing-library/react";
import { App } from "./App";

test("приложение показывает заголовок", () => {
  render(<App />);
  expect(screen.getByText("mnogobase")).toBeTruthy();
});
