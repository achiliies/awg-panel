# AWG Panel container image.
#
# The image carries the panel only. The AmneziaWG kernel module and the
# awg / awg-quick binaries belong to the host: a container cannot load a
# kernel module, and the tools must match the module the host is running.
# docker-compose.yml bind-mounts them in.
#
# Build the UI first so the image needs no Node:
#   make -C panel build
#   docker build -t awg-panel -f panel/Dockerfile panel

# Pinned by digest, not only by tag. python:3.12-slim is a moving target: it
# picks up a new Python patch, a new Debian base and a new OpenSSL every time
# upstream rebuilds it, so the same commit built twice was not the same image
# and nothing said which one a container was running. This is the multi-arch
# index digest, so amd64 and arm64 both resolve from it. Move it on purpose,
# the way the dependency locks move.
FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS runtime

# iproute2 for `ip link show`, procps for the /proc reads the collector does,
# ca-certificates for the update check. Nothing else: the image should not be
# able to do more than the panel needs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 procps ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=awgui.settings \
    AWG_CONF_DIR=/etc/amnezia/amneziawg \
    AWG_PANEL_DATA=/var/lib/awg-panel \
    AWG_PANEL_ENV=/dev/null \
    AWG_PANEL_LISTEN=0.0.0.0 \
    AWG_PANEL_PORT=8088 \
    AWG_PANEL_BASE_PATH=/

WORKDIR /opt/awg-panel

# The locks, never requirements.txt: exact versions and a sha256 for every
# file, transitive dependencies included. Copied before the source so this
# layer is cached until a dependency actually moves.
COPY requirements-bootstrap.lock requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements-bootstrap.lock \
    && pip install --no-cache-dir --require-hashes -r requirements.lock

# Everything panel/.dockerignore has not held back. That file is what stands
# between this line and whatever the person building left lying in panel/.
COPY . .

# Fail the build rather than shipping an image that serves a blank page.
RUN test -f frontend/dist/index.html \
    || (echo "frontend/dist is missing - run 'make -C panel build' first" >&2 && exit 1)

# And fail rather than shipping an image carrying somebody's development
# machine. .dockerignore is one edit away from being wrong at any time, and the
# way it goes wrong is silent: a build that succeeds, an image that works, and a
# signing key and account database inside it that nobody has a reason to look
# for. Checked here, where the answer is about the image itself rather than
# about a list of patterns being right.
RUN for leak in .venv .dev .demo .lockgen node_modules db.sqlite3 secret.key; do \
        if [ -n "$(find . -name "$leak" -print -quit)" ]; then \
            echo "refusing to build: $leak reached the image." >&2; \
            echo "panel/.dockerignore should have excluded it." >&2; \
            exit 1; \
        fi; \
    done

RUN mkdir -p /var/lib/awg-panel && chmod 700 /var/lib/awg-panel

EXPOSE 8088
VOLUME ["/var/lib/awg-panel"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import os,urllib.request,sys; \
base=os.environ.get('AWG_PANEL_BASE_PATH','/'); \
port=os.environ.get('AWG_PANEL_PORT','8088'); \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}{base}api/v1/health', timeout=4).status == 200 else 1)"

CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn -c deploy/gunicorn.conf.py awgui.wsgi:application"]
