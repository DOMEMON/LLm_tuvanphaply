FROM node:24.20.0-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

ARG VITE_API_BASE_URL=http://localhost:8000/api/v1
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
ARG VITE_APP_STAGE=G1
ENV VITE_APP_STAGE=${VITE_APP_STAGE}
ARG VITE_REQUIRE_ACCOUNT=false
ENV VITE_REQUIRE_ACCOUNT=${VITE_REQUIRE_ACCOUNT}
ARG VITE_VISITOR_MODE=false
ENV VITE_VISITOR_MODE=${VITE_VISITOR_MODE}

RUN npm run build

FROM nginx:1.29-alpine AS runtime

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 3000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
  CMD wget -qO- http://127.0.0.1:3000/ >/dev/null || exit 1
