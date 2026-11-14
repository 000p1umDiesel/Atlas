import type { EntityTypeInfo, TypeCount } from "../api/types";
import { themedCss } from "./communityPalette";
import { typeColorAt, typeColorIndex } from "./typePalette";

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
  /** Ключи типов, у которых есть сущности (`count > 0`), в том же порядке. */
  present: string[];
  typeInfo(type: string): TypeInfo;
  /** Русская подпись; для неизвестного типа — сам ключ. */
  typeLabel(type: string): string;
  /** CSS-цвет типа для DOM (`light-dark(...)`); неизвестный тип и «Другое» — `var(--t0)`. */
  typeColor(type: string): string;
}

export function createTypeCatalog(types: readonly TypeCount[] | undefined): TypeCatalog {
  const list = types ?? [];
  const order = list.map((t) => t.type);
  const byType = new Map(list.map((t) => [t.type, t]));
  const typeInfo = (type: string): TypeInfo => {
    const hit = byType.get(type);
    if (!hit) return { type, label: type, description: "", known: false };
    const label = hit.label.trim() || type;
    return { type, label, description: hit.description.trim(), known: true };
  };
  return {
    types: list,
    order,
    present: list.filter((t) => t.count > 0).map((t) => t.type),
    typeInfo,
    typeLabel: (type) => typeInfo(type).label,
    typeColor: (type) => {
      const slot = typeColorIndex(type, order);
      return slot < 0 ? "var(--t0)" : themedCss(typeColorAt(slot));
    },
  };
}
