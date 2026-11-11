FROM eclipse-temurin:21-jre

ARG FUSEKI_VERSION=6.2.0
ARG FUSEKI_SHA512=ba65f5867d2d4741b2ed9e2af5a0d4fbb447909894ab2a0c6bc4dac8997f4fe339c87b13c48d45d054977769f0f8bf763ea346b1f7792d5cdc458041bd43a132

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL -o /tmp/fuseki.tar.gz "https://archive.apache.org/dist/jena/binaries/apache-jena-fuseki-${FUSEKI_VERSION}.tar.gz" \
 && echo "${FUSEKI_SHA512}  /tmp/fuseki.tar.gz" | sha512sum -c - \
 && mkdir -p /opt/fuseki /fuseki/databases \
 && tar -xzf /tmp/fuseki.tar.gz -C /opt/fuseki --strip-components=1 \
 && rm /tmp/fuseki.tar.gz

ENV FUSEKI_HOME=/opt/fuseki \
    FUSEKI_BASE=/fuseki/run \
    JVM_ARGS=-Xmx2g

WORKDIR /fuseki
EXPOSE 3030
ENTRYPOINT ["/opt/fuseki/fuseki-server"]
CMD ["--config=/fuseki/config/kl_dataset_config.ttl"]
