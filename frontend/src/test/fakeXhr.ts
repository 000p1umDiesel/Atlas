// Подмена XMLHttpRequest для тестов загрузки: тест сам двигает прогресс и отвечает на запрос.

type Listener = ((event: ProgressEvent) => void) | null;

export class FakeXhr {
  /** Все созданные запросы по порядку (сбрасывает `installFakeXhr`). */
  static instances: FakeXhr[] = [];

  method = "";
  url = "";
  body: unknown = undefined;
  sent = false;
  done = false;
  withCredentials = false;
  status = 0;
  responseText = "";
  upload: { onprogress: Listener } = { onprogress: null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor() {
    FakeXhr.instances.push(this);
  }

  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }

  send(body: unknown) {
    this.body = body;
    this.sent = true;
  }

  /** Прогресс отправки тела. */
  progress(loaded: number, total: number, lengthComputable = true) {
    this.upload.onprogress?.({ loaded, total, lengthComputable } as ProgressEvent);
  }

  /** Ответ сервера: тело — JSON (или строка как есть). */
  respond(status: number, body: unknown) {
    this.status = status;
    this.responseText = typeof body === "string" ? body : JSON.stringify(body);
    this.done = true;
    this.onload?.();
  }

  /** Сетевой сбой. */
  fail() {
    this.done = true;
    this.onerror?.();
  }
}

/** Ставит FakeXhr вместо XMLHttpRequest; снять — `vi.unstubAllGlobals()`. */
export function installFakeXhr(): FakeXhr[] {
  FakeXhr.instances = [];
  vi.stubGlobal("XMLHttpRequest", FakeXhr);
  return FakeXhr.instances;
}

/** Запросы, которые отправлены и ещё без ответа. */
export function inFlight(): FakeXhr[] {
  return FakeXhr.instances.filter((xhr) => xhr.sent && !xhr.done);
}
