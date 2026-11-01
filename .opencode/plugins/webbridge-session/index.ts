import { Plugin } from "@opencode/plugin"
import { readFile } from "node:fs/promises"
import { homedir } from "node:os"
import { join } from "node:path"

const BASE_URL = "http://127.0.0.1:8000"
const TOKEN_PATH = join(homedir(), ".webbridge", "api_token")

async function readToken(): Promise<string | undefined> {
  const fromEnv = process.env.WEBBRIDGE_API_TOKEN
  if (fromEnv) return fromEnv
  try {
    const token = await readFile(TOKEN_PATH, "utf8")
    return token.trim() || undefined
  } catch {
    return undefined
  }
}

export default Plugin.define({
  id: "webbridge-session",

  async setup(ctx) {
    const token = await readToken()

    await ctx.session.hook(
      "http.request",
      async (event) => {
        const url = new URL(event.request.url)
        if (url.origin !== BASE_URL) return

        // opencode сам выполняет инструменты и ведёт агентный цикл.
        // Мост должен лишь сохранить привязку сессии к удалённому чату
        // и не смешать title-запрос с основным диалогом.
        if (event.kind === "primary") {
          event.request.headers.set("X-WebBridge-Session-ID", event.sessionID)
          event.request.headers.set("X-WebBridge-Kind", "primary")
        } else if (event.kind === "title") {
          event.request.headers.set("X-WebBridge-Kind", "title")
        }

        if (token) {
          event.request.headers.set("Authorization", `Bearer ${token}`)
        }
      },
      { providerID: "webbridge" },
    )
  },
})
