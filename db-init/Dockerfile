FROM postgres:16-alpine

RUN apk add --no-cache unzip \
    && wget -O /tmp/demo.zip https://edu.postgrespro.ru/demo-small.zip \
    && cd /tmp && unzip demo.zip \
    && sed -i 's/DROP DATABASE demo/DROP DATABASE IF EXISTS demo/' /tmp/demo-small*.sql \
    && mv /tmp/demo-small*.sql /docker-entrypoint-initdb.d/ \
    && rm -f /tmp/demo.zip
