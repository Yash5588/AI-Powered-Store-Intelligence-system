# Multi-stage build: install deps + build the Vite app, then serve with nginx.
# The resulting image is tiny (~25 MB) and needs no Node runtime at serving time.
FROM node:20-alpine AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

# The API base URL the *browser* will call. Inside docker-compose the browser
# talks to localhost:8000 (the host-mapped port), not to the compose service
# name, because the request originates in the user's browser, not the container.
ARG VITE_API_BASE_URL=http://localhost:8000
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL

COPY index.html vite.config.js ./
COPY src/ ./src/
RUN npm run build

# ---- Serve with nginx (Alpine) ----
FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html

# SPA fallback: send all non-file routes to index.html.
RUN printf 'server {\n\
    listen 3000;\n\
    root /usr/share/nginx/html;\n\
    location / {\n\
        try_files $uri $uri/ /index.html;\n\
    }\n\
}\n' > /etc/nginx/conf.d/default.conf

EXPOSE 3000
CMD ["nginx", "-g", "daemon off;"]
