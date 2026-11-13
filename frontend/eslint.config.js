import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["node_modules", "../src/mnogobase/web/dist"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  // Правила хуков: порядок вызовов — ошибка, неполные зависимости эффектов — предупреждение
  // (осознанные исключения помечены комментарием на месте). Правила React Compiler из
  // recommended v7 не включаем: компилятор в проекте не используется.
  {
    files: ["src/**/*.{ts,tsx}"],
    plugins: { "react-hooks": reactHooks },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
    },
  },
  // Скрипты сборки работают в Node.
  { files: ["scripts/**/*.mjs"], languageOptions: { globals: globals.node } },
);
