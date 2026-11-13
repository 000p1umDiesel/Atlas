import type { EntityTypeInfo, TypeCount } from "../api/types";
import { themedCss } from "./communityPalette";
import { typeColorAt } from "./typePalette";

export interface TypeInfo extends EntityTypeInfo {
  /** Тип есть в списке настроенных (иначе подпись — ключ, определения нет, цвет нейтральный). */
  known: boolean;
}

/** Справочник типов сущностей: единый источник подписей, определений и цветов. */
export interface TypeCatalog {
  /** Типы с числами, в порядке `extract.entity_types`. */
  types: readonly TypeCount[];
  /** Ключи типов в том же порядке: по нему назначаются цвета. */
  order: string[];
  typeInfo(type: string): TypeInfo;
  /** Русская подпись; для неизвестного типа — сам ключ. */
  typeLabel(type: string): string;
  /** CSS-цвет типа для DOM (`light-dark(...)`); неизвестный тип — `var(--t0)`. */
  typeColor(type: string): string;
}

export function createTypeCatalog(types: readonly TypeCount[] | undefined): TypeCatalog {
  const list = types ?? [];
  const order = list.map((t) => t.type);
  const byType = new Map(list.map((t, i) => [t.type, { t, i }]));
  const typeInfo = (type: string): TypeInfo => {
    const hit = byType.get(type);
    if (!hit) return { type, label: type, description: "", known: false };
    const label = hit.t.label.trim() || type;
    return { type, label, description: hit.t.description.trim(), known: true };
  };
  return {
    types: list,
    order,
    typeInfo,
    typeLabel: (type) => typeInfo(type).label,
    typeColor: (type) => {
      const hit = byType.get(type);
      return hit ? themedCss(typeColorAt(hit.i)) : "var(--t0)";
    },
  };
}
