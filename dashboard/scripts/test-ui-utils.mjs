import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import ts from "typescript";
async function load(name) {
  const source = await fs.readFile(
    new URL("../src/utils/" + name, import.meta.url),
    "utf8",
  );
  const js = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  return import(
    "data:text/javascript;base64," + Buffer.from(js).toString("base64")
  );
}
const { csvCell, csvContent } = await load("csv.ts");
const { normalizeSearch, navigationItems } = await load("navigation.ts");
test("CSV neutralizes formulas in user-controlled names", () => {
  for (const name of [
    '=HYPERLINK("https://example.invalid")',
    "+SUM(1,2)",
    "-cmd",
    "@SUM(1,2)",
    "  =1+1",
    "\t@SUM(1)",
  ])
    assert.ok(csvCell(name).startsWith("\"'"));
});
test("CSV preserves numeric negative balances as numbers", () =>
  assert.equal(csvCell(-12.5), '"-12.5"'));
test("CSV handles multiline names, commas, quotes and Persian text", () => {
  assert.equal(
    csvCell('مینا, "احمدی"\nردیف دوم'),
    '"مینا, ""احمدی""\nردیف دوم"',
  );
  assert.equal(
    csvContent([
      ["نام", "موجودی"],
      ["مینا", 12.5],
    ]),
    '\uFEFF"نام","موجودی"\r\n"مینا","12.5"',
  );
});
test("Arabic variants find Persian navigation labels", () =>
  assert.equal(normalizeSearch("  كاربران  "), normalizeSearch("کاربران")));
test("Navigation destinations remain unique and local", () => {
  assert.equal(
    new Set(navigationItems.map((i) => i.to)).size,
    navigationItems.length,
  );
  assert.ok(
    navigationItems.every(
      (i) => i.to.startsWith("/") && !i.to.startsWith("//"),
    ),
  );
});
