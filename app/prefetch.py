"""Скачивает модели ИИ заранее (зовёт установщик), чтобы первый скан не ждал загрузки."""
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main() -> int:
    try:
        from transformers import ClapModel, ClapProcessor, CLIPModel, CLIPProcessor
    except Exception as e:  # noqa: BLE001
        print("transformers не установлен, пропускаю:", e)
        return 0
    ok = True
    for name, M, P in (("openai/clip-vit-base-patch32", CLIPModel, CLIPProcessor),
                       ("laion/clap-htsat-unfused", ClapModel, ClapProcessor)):
        print(f"Скачиваю модель {name} ...", flush=True)
        try:
            M.from_pretrained(name)
            P.from_pretrained(name)
        except Exception as e:  # noqa: BLE001
            ok = False
            print("Не получилось (скачается при первом скане):", e, flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
