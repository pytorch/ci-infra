# Holds one BuildKit slot (~10 min) so a burst of parallel builds exceeds the
# warm baseline and forces KEDA scale-up. CACHEBUST differs per build so each
# actually runs (no layer reuse).
FROM alpine:3.21
ARG CACHEBUST
RUN echo "osdc buildkit scale-test ${CACHEBUST}" && sleep 600
