FROM nginx:stable-alpine

COPY deploy/nginx.conf /etc/nginx/nginx.conf

USER 10001:10001
