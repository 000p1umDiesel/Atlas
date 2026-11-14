import { ChunkPanel } from "../panels/ChunkPanel";
import { EntityPanel } from "../panels/EntityPanel";
import { usePanel } from "./panel";
import { Sheet } from "./Sheet";

/**
 * Правая панель всех разделов, кроме графа (там своя карточка): что показать — в
 * search-параметре `panel`, поэтому её можно открыть ссылкой, а «Назад» её не открывает
 * снова (смена параметра — `replace`). Крестик и Esc убирают параметр.
 */
export function PanelHost() {
  const { panel, close } = usePanel();
  return (
    <Sheet open={panel !== null} onClose={close} title={panel?.kind === "entity" ? "Сущность" : "Фрагмент"}>
      {panel?.kind === "chunk" && <ChunkPanel key={panel.id} id={panel.id} />}
      {panel?.kind === "entity" && <EntityPanel key={panel.id} id={panel.id} />}
    </Sheet>
  );
}
