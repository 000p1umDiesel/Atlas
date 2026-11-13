import { generateFixtures } from "./generate";

const CONFIG_TYPES = [
  "Person",
  "Organization",
  "Location",
  "Event",
  "Project",
  "Product",
  "Equipment",
  "Software",
  "Technology",
  "Method",
  "Concept",
  "Work",
  "Metric",
  "Regulation",
  "Substance",
  "Condition",
  "Organism",
  "Other",
];

describe("generateFixtures", () => {
  const f = generateFixtures(7);
  it("детерминирован", () => {
    expect(generateFixtures(7)).toEqual(f);
  });
  it("id уникальны, рёбра ссылаются на существующие узлы", () => {
    const ids = f.graph.nodes.map((n) => n.entity_id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(ids.length).toBe(300);
    const set = new Set(ids);
    for (const r of f.graph.relations) {
      expect(set.has(r.src_id)).toBe(true);
      expect(set.has(r.dst_id)).toBe(true);
    }
  });
  it("типы — все 18 из config.yaml в его порядке, у каждого есть узлы; 12 сообществ", () => {
    expect(f.types.map((t) => t.type)).toEqual(CONFIG_TYPES);
    for (const t of f.types) {
      expect(t.count).toBeGreaterThanOrEqual(1);
      expect(f.graph.nodes.filter((n) => n.type === t.type)).toHaveLength(t.count);
    }
    expect(f.types).toHaveLength(18);
    expect(new Set(f.graph.nodes.map((n) => n.type)).size).toBe(18);
    expect(new Set(f.graph.nodes.map((n) => n.community)).size).toBe(12);
  });
  it("у типа русская подпись и определение (форма API этапа 2)", () => {
    const person = f.types.find((t) => t.type === "Person")!;
    expect(person.label).toBe("Человек");
    expect(person.description).toBe("Реальный или вымышленный человек, названный по имени или однозначно узнаваемый.");
    expect(f.types.find((t) => t.type === "Software")!.label).toBe("ПО");
    expect(f.types.find((t) => t.type === "Regulation")!.label).toBe("Нормативный акт");
    expect(f.types.find((t) => t.type === "Condition")).toMatchObject({
      label: "Состояние",
      description: "Болезнь, расстройство, симптом или иное медицинское либо психологическое состояние.",
    });
    expect(f.types.find((t) => t.type === "Organism")).toMatchObject({
      label: "Организм",
      description: "Вид, организм, тип клеток, орган или иное живое существо либо его часть.",
    });
    expect(f.types.find((t) => t.type === "Other")).toMatchObject({
      label: "Другое",
      description: "Всё значимое, что не подходит ни под один тип выше.",
    });
    for (const t of f.types) {
      expect(t.label).toMatch(/^[А-ЯЁ]/);
      expect(t.description.length).toBeGreaterThan(20);
    }
  });
  it("распределение правдоподобное: понятий больше, чем веществ", () => {
    const count = (type: string) => f.types.find((t) => t.type === type)!.count;
    for (const big of ["Concept", "Person", "Organization", "Method", "Work"])
      for (const small of ["Regulation", "Substance", "Equipment", "Condition", "Organism", "Other"])
        expect(count(big)).toBeGreaterThan(count(small));
  });
  it("статистика согласована с графом (ruling E)", () => {
    expect(f.stats.entities).toBe(300);
    expect(f.stats.relations).toBe(f.graph.relations.length);
    expect(f.types.reduce((s, t) => s + t.count, 0)).toBe(300);
  });
});
