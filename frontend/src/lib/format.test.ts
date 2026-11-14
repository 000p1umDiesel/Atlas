import { ago, countOf, formatInt, formatScore, LABEL_MAX_CHARS, plural, seconds, truncateLabel } from "./format";

const DOCS: [string, string, string] = ["документ", "документа", "документов"];

test("русские формы множественного числа", () => {
  expect([1, 2, 5, 11, 12, 21, 22, 25, 111, 104].map((n) => plural(n, DOCS))).toEqual([
    "документ",
    "документа",
    "документов",
    "документов",
    "документов",
    "документ",
    "документа",
    "документов",
    "документов",
    "документа",
  ]);
});

test("числа в русской записи", () => {
  expect(formatInt(1260)).toBe((1260).toLocaleString("ru-RU"));
  expect(formatInt(1260)).not.toBe("1260");
  expect(formatScore(0.041234)).toBe("0,0412");
  expect(countOf(1260, ["фрагмент", "фрагмента", "фрагментов"])).toBe(`${formatInt(1260)} фрагментов`);
});

test("countOf: родительный падеж после «из» (карточка сообщества)", () => {
  const forms: [string, string, string] = ["участника", "участников", "участников"];
  expect([1, 2, 5, 11, 21, 22, 111].map((n) => countOf(n, forms))).toEqual([
    "1 участника",
    "2 участников",
    "5 участников",
    "11 участников",
    "21 участника",
    "22 участников",
    "111 участников",
  ]);
});

describe("ago и seconds", () => {
  const NOW = new Date("2026-11-14T12:00:00Z");
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });
  afterEach(() => vi.useRealTimers());
  const now = NOW.getTime() / 1000;

  test("ago: относительное время по-русски, null — «никогда»", () => {
    expect(ago(null)).toBe("никогда");
    expect(ago(now)).toBe("сейчас");
    expect(ago(now - 30)).toBe("30 секунд назад");
    expect(ago(now - 5 * 60)).toBe("5 минут назад");
    expect(ago(now - 3 * 3600)).toBe("3 часа назад");
    expect(ago(now - 86400)).toBe("вчера");
    expect(ago(now - 5 * 86400)).toBe("5 дней назад");
    expect(ago(now + 120)).toBe("через 2 минуты");
  });

  test("seconds: меньше 0,1 с и дробь с запятой", () => {
    expect(seconds(42)).toBe("<0,1 с");
    expect(seconds(1234)).toBe("1,2 с");
    expect(seconds(3000)).toBe("3 с");
  });
});

test("truncateLabel: длинная подпись узла режется с многоточием", () => {
  expect(truncateLabel("Сбер")).toBe("Сбер");
  const long = "Отчёт State of AI (RU) по рынку генеративных моделей";
  const short = truncateLabel(long);
  expect(Array.from(short)).toHaveLength(LABEL_MAX_CHARS);
  expect(short.endsWith("…")).toBe(true);
  expect(truncateLabel("abcdef", 4)).toBe("abc…");
  // Пробел перед многоточием не остаётся; суррогатные пары не разрываются.
  expect(truncateLabel("ab cdef", 4)).toBe("ab…");
  expect(truncateLabel("😀😀😀😀😀", 3)).toBe("😀😀…");
});
