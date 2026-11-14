import { useRouter } from "@tanstack/react-router";
import type { AnchorHTMLAttributes, MouseEvent } from "react";

/** Обычный клик левой кнопкой без модификаторов: Ctrl/Cmd/Shift/Alt и средняя кнопка — браузеру. */
export function isPlainClick(event: MouseEvent): boolean {
  return (
    !event.defaultPrevented &&
    event.button === 0 &&
    !event.metaKey &&
    !event.ctrlKey &&
    !event.shiftKey &&
    !event.altKey
  );
}

type Props = Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href"> & { href: string };

/**
 * Ссылка на внутренний адрес, который роутер ещё не знает как маршрут с типами
 * (`/documents/<id>/read?chunk=…`, `/wiki/<slug>`): переход роутером без перезагрузки,
 * открытие в новой вкладке — как у обычной ссылки.
 */
export function RouterAnchor({ href, onClick, ...rest }: Props) {
  const router = useRouter();
  return (
    <a
      href={href}
      onClick={(event) => {
        onClick?.(event);
        if (!isPlainClick(event)) return;
        event.preventDefault();
        void router.navigate({ href });
      }}
      {...rest}
    />
  );
}
