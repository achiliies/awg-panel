import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "node_modules", "coverage"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_", caughtErrorsIgnorePattern: "^_" },
      ],
      "@typescript-eslint/no-explicit-any": "error",
      "@typescript-eslint/consistent-type-imports": [
        "warn",
        { prefer: "type-imports", fixStyle: "inline-type-imports" },
      ],
      eqeqeq: ["error", "smart"],
      // The panel is served to operators, not developers: console noise in
      // production hides the warnings that matter.
      "no-console": ["warn", { allow: ["warn", "error"] }],
    },
  },
  {
    /*
     * Nothing new gets pinned to the bottom edge without telling the toast
     * stack about it.
     *
     * The stack sits in that corner, so anything parked there is underneath it.
     * The save bar was written out by hand twice, in ServerConfig and in
     * Settings, and both copies spent five seconds at a time covering their own
     * Save button. StickyActionBar is now the one that exists and it reserves
     * its height; a third copy written from the class names up would be the
     * same bug again, and nothing but this would catch it.
     */
    files: ["src/**/*.tsx"],
    ignores: ["src/components/StickyActionBar.tsx", "src/components/ui/toast.tsx"],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector:
            "Literal[value=/^(?=.*\\b(?:sticky|fixed)\\b)(?=.*\\bbottom-0\\b).*$/]," +
            "TemplateElement[value.raw=/^(?=.*\\b(?:sticky|fixed)\\b)(?=.*\\bbottom-0\\b)[\\s\\S]*$/]",
          message:
            "Pinning something to the bottom edge puts it under the toast stack. Use " +
            "StickyActionBar, which reserves its own height, or call useBottomInset from " +
            "@/lib/toast-space so notifications move above it.",
        },
      ],
    },
  },
  {
    // The entry module mounts the tree instead of exporting components, so it
    // is outside the fast-refresh graph by definition.
    files: ["src/main.tsx"],
    rules: { "react-refresh/only-export-components": "off" },
  },
  {
    // Build tooling runs in Node, not in the browser.
    files: ["*.config.js", "*.config.ts"],
    extends: [js.configs.recommended],
    languageOptions: { globals: globals.node },
  },
);
