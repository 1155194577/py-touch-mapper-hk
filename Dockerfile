FROM python:3.11-slim-bookworm

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ant bzip2 ca-certificates curl git openjdk-17-jdk-headless nodejs libcairo2 \
    libgl1 libglu1-mesa libgomp1 libsm6 libxi6 libxinerama1 libxrender1 libxxf86vm1 \
    && rm -rf /var/lib/apt/lists/*

ENV TOUCH_MAPPER_RUNTIME=/opt/touch-mapper
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/touch-mapper
RUN git init -q . \
    && git fetch --depth 1 https://github.com/skarkkai/touch-mapper.git e2710eac904ed5d0d17d918a132beb496cbf80d4 \
    && git checkout --detach FETCH_HEAD \
    && ant -f OSM2World/build.xml clean jar \
    && ln -s ../OSM2World converter/OSM2World

RUN archive=Blender2.78/blender-2.78c-linux-glibc219-x86_64.tar.bz2 \
    && { curl -fsSL --retry 3 -o /tmp/blender.tar.bz2 "https://download.blender.org/release/$archive" \
      || curl -fsSL --retry 3 -o /tmp/blender.tar.bz2 "https://ftp.halifax.rwth-aachen.de/blender/release/$archive"; } \
    && echo '282f5ef2ae7a342ef20b9892a903bd18225c10c77335cdb58b4e88e3f0e71837  /tmp/blender.tar.bz2' | sha256sum -c - \
    && mkdir blender \
    && tar -xjf /tmp/blender.tar.bz2 --strip-components=1 -C blender \
    && rm /tmp/blender.tar.bz2 \
    && ln -s ../blender converter/blender \
    && python -m pip install --no-cache-dir --no-compile --target blender/2.78/python/lib/python3.5/svgwrite \
       svgwrite==1.1.9 pyparsing==2.4.7

WORKDIR /opt/py-touch-mapper
COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
RUN python -m pip install --no-cache-dir .

WORKDIR /work
USER 65534:65534
ENTRYPOINT ["touch-mapper"]
