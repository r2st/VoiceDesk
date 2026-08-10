# syntax=docker/dockerfile:1
#
# Next.js dashboard, built with `output: "standalone"` so the runtime image
# ships only the server bundle and the node_modules it traced as actually
# used, not the full workspace.

FROM node:22-slim AS deps

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:22-slim AS builder

WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# The dashboard calls the API directly from the browser (see next.config.mjs),
# so this is baked in at build time like any other client-side env var.
ARG NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL}

RUN npm run build

FROM node:22-slim AS runtime

RUN groupadd --system voicedesk && useradd --system --gid voicedesk --create-home voicedesk

WORKDIR /app
ENV NODE_ENV=production \
    PORT=3011 \
    HOSTNAME=0.0.0.0

COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
# No `public/` directory exists in this project yet; add
# `COPY --from=builder /app/public ./public` back here if one is introduced.

USER voicedesk

EXPOSE 3011

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD node -e "fetch('http://localhost:3011/login').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"

CMD ["node", "server.js"]
