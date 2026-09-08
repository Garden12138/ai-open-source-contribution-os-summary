# Trusted Docker supervisor only; repository code runs in separate Runner containers.
FROM docker:29-cli@sha256:eccaacfeed644c7de222ff047483568cb988dde95476fbaaf10ea2d04921bb66 AS docker-cli
# Compose supplies the freshly built API image through this named context.
FROM contribos-app

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

USER 10001:10001
ENTRYPOINT ["contribos"]
CMD ["sandbox-worker"]
