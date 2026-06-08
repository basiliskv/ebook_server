# Ebook Server

Local ebook/media server for browsing archives, PDFs, standalone media files, and Eagle libraries.

## Setup

Install dependencies:

```sh
pip install -r requirements.txt
```

Copy the example config and edit paths for your machine:

```sh
cp local_config.example.json local_config.json
```

`local_config.json` is intentionally ignored by Git. A minimal config looks like this:

```json
{
  "defaultProfile": "default",
  "defaultLibrary": "books",
  "cacheRoot": ".cache",
  "profileOptions": {
    "default": {
      "queryPlusAsSpace": false
    }
  },
  "profiles": {
    "default": {
      "books": "/path/to/your/books",
      "eagle": "/path/to/your/Eagle.library"
    }
  }
}
```

`profileOptions.<profile>.queryPlusAsSpace` controls how `+` in query parameters is interpreted.
- `false`: keep `+` as a literal plus sign. This is safer on Windows when filenames contain `+`.
- `true`: decode `+` as space, which may match some macOS/browser flows.

Run:

```sh
python app.py --profile default --host 0.0.0.0 --port 8000
```

You can also override config locations with environment variables:

```sh
EBOOK_SERVER_CONFIG=/path/to/local_config.json EBOOK_CACHE_ROOT=/path/to/cache python app.py
```
