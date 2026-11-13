import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["node_modules", "../src/mnogobase/web/dist"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  // Скрипты сборки работают в Node.
  { files: ["scripts/**/*.mjs"], languageOptions: { globals: globals.node } },
);
