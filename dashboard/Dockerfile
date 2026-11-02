# Production build of the dashboard SPA, served as static files by nginx.
# The supervisor (dashboard_api) is NOT part of this image - it spawns Chrome
# and the agent on the host, so it must keep running there. This image only
# replaces `npm run dev` for deploy; use `npm run dev` for hot-reload work.

FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
# The same defaults compose.yaml passes, so an image run on its own (without
# compose's environment) still points at the real supervisor port instead of
# one nothing listens on. compose.yaml overrides every one of these anyway.
ENV SUPERVISOR_HOST=127.0.0.1
ENV SUPERVISOR_PORT=28080
ENV DASHBOARD_LISTEN_PORT=25173
# These two must exist in the environment for the entrypoint's envsubst to
# substitute them at all - an unset name is left as a literal `${...}` in the
# rendered config and nginx then refuses to start.
ENV DASHBOARD_LISTEN_ADDR=127.0.0.1
ENV DASHBOARD_LOCAL_PORT=25174
EXPOSE 25173
