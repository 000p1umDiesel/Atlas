import { fromSlider, sliderScale, toSlider } from "./sliderScale";

describe("sliderScale", () => {
  it("целые значения → шаг 1, максимум из данных", () => {
    expect(sliderScale([3, 1, 27, 8])).toEqual({ max: 27, step: 1 });
  });
  it("дробные значения → шаг около сотой доли максимума", () => {
    expect(sliderScale([0.12, 0.5, 2.4])).toEqual({ max: 2.4, step: 0.01 });
    expect(sliderScale([0.5, 12.3])).toEqual({ max: 12.3, step: 0.1 });
  });
  it("пустые и неконечные значения не ломают шкалу", () => {
    expect(sliderScale([])).toEqual({ max: 0, step: 1 });
    expect(sliderScale([Number.NaN, Number.POSITIVE_INFINITY, 4])).toEqual({ max: 4, step: 1 });
  });
});

describe("toSlider/fromSlider", () => {
  const scale = { max: 40, step: 1 };
  it("квадратичная шкала: середина ползунка — четверть максимума", () => {
    expect(fromSlider(50, scale)).toBe(10);
    expect(toSlider(10, scale)).toBe(50);
  });
  it("края: 0 и максимум из данных; значение больше максимума — в правом краю", () => {
    expect(fromSlider(0, scale)).toBe(0);
    expect(fromSlider(100, scale)).toBe(40);
    expect(toSlider(1000, scale)).toBe(100);
    expect(toSlider(0, { max: 0, step: 1 })).toBe(0);
  });
  it("округление к шагу без хвостов плавающей точки", () => {
    const s = { max: 2.4, step: 0.01 };
    expect(fromSlider(37, s)).toBe(0.33);
    expect(String(fromSlider(71, s))).toBe("1.21");
  });
  it("слева шаг мелкий: можно выбрать вес 1 из 40", () => {
    const values = Array.from({ length: 101 }, (_, p) => fromSlider(p, scale));
    expect(values).toContain(1);
    expect(values).toContain(2);
  });
});
