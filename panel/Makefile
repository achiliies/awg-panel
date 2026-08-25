# Developer targets for the panel.
#
# Everything here runs against the mock controller and a throwaway config tree
# under .dev/, so it needs no root, no kernel module and no live tunnel. On a
# server use `awg-panel` instead: it drives the installed copy in
# /opt/awg-panel with the real /etc/awg-panel.env.
#
# To point a target at the real installation, clear the sandbox:
#     make migrate DEV_ENV=

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

PY ?= python3
VENV ?= .venv
DEV_DIR ?= .dev
API_PORT ?= 8088
UI_PORT ?= 5173
PYTEST_ARGS ?= -q

# The demo panel's port, and the whole of its address: `make demo` serves the
# built SPA and the API from one process, so unlike `make dev` there is no
# second port and no proxy between them. Fixed rather than derived from the
# worktree, because the point of it is a URL that is the same on every machine
# the target is ever run on.
DEMO_PORT ?= 8099
# Every interface. `make dev` is loopback-only and stays that way; this target
# exists to be looked at from somewhere else, which on a VM or a remote box is
# the only way to look at it at all.
DEMO_HOST ?= 0.0.0.0
# The demo's own sandbox, and not $(DEV_DIR), because this one is deleted.
# Sharing the directory would mean a demo somebody Ctrl-C'd took away the
# database, the config tree and the clients of whoever was using `make dev`.
DEMO_DIR ?= .demo

# Where under the port the panel answers. The root unless asked otherwise:
#
#     make demo                     http://10.0.0.2:8099/
#     make demo DEMO_PATH=random    http://10.0.0.2:8099/demo/ka9v3ujqr2t7wm1xf/
#     make demo DEMO_PATH=/demo/    http://10.0.0.2:8099/demo/
#
# The root by default because that is the URL somebody can be told over the
# phone, and because this target is meant to be typed with nothing after it.
# The installed panel does mount under a generated path - install-panel.sh
# writes one into /etc/awg-panel.env - so `random` is how you look at that
# layout, and it is worth looking at: it is the one part of the deployment shape
# the demo can show that a root-mounted run cannot.
#
# What a path buys beyond that is modest and worth stating plainly. Everything
# outside it is an empty 404, so a port scanner does not get handed a login
# form, and the URL cannot be guessed by somebody who knows only the port. It is
# not authentication - the login form is - and it is no help at all once the URL
# has been sent to anybody.
#
# Seventeen characters, from an alphabet of thirty-six, is about 88 bits. The
# number is not load-bearing against an attacker; it is long enough that the
# path is not stumbled into and short enough to stay on one line of a terminal
# next to a scheme, an address and a port.
DEMO_PATH ?=
DEMO_SECRET_CHARS := 17

ifeq ($(strip $(DEMO_PATH)),)
DEMO_BASE_PATH := /
else ifeq ($(strip $(DEMO_PATH)),random)
# `:=` and not `?=`, because a recursive variable re-runs the shell on every
# expansion: the environment, the printed URLs and the cookie scope would each
# get a different path. A value given on the command line still wins.
DEMO_SECRET := $(shell head -c 512 /dev/urandom | LC_ALL=C tr -dc 'a-z0-9' | cut -c1-$(DEMO_SECRET_CHARS))
DEMO_BASE_PATH := /demo/$(DEMO_SECRET)/
else
# Anything else is taken literally, since the variable is named for a path and
# refusing one would be a surprise. Normalised to exactly one slash at each end,
# because AWG_PANEL_BASE_PATH is joined to route prefixes and cookie scopes that
# both assume that shape - and `demo`, `/demo` and `demo/` are all things a
# person types meaning the same directory.
DEMO_BASE_PATH := $(shell p='$(strip $(DEMO_PATH))'; p="/$${p#/}"; printf '%s' "$${p%/}/")
endif

# What the demo puts on the wire. Three states, and the one that asks nothing
# of whoever runs the target is the default:
#
#     make demo                                        plain HTTP
#     make demo DEMO_TLS=self                          a certificate per run
#     make demo DEMO_TLS=cert.pem DEMO_TLS_KEY=key.pem a certificate you have
#
# Plain by default because the job of this target is to be typed without
# arguments and looked at, and every form of TLS asks for something first. A
# generated certificate costs a browser warning on every machine the demo is
# shown to, and a real one has to exist before the command is run. Neither
# belongs in front of somebody who wanted to see the dashboard.
#
# What makes that acceptable is what the target already is: mock data, a
# database deleted when it stops, and a password generated for one run and
# valid nowhere else. Nothing here is worth intercepting on its own account.
#
# What does not survive that argument is the network the demo is shown over.
# The password is printed on one machine and typed on another, the session
# cookie rides the whole run, and the clients page hands out a private key per
# client - in the clear, all of it, to anything on the LAN in between. So the
# target says so, in as many words, whenever it starts without TLS, and the two
# states below are the answer when the wire is not one you trust.
DEMO_TLS ?=
DEMO_TLS_KEY ?=

DEMO_TLS_DIR := $(CURDIR)/$(DEMO_DIR)/tls

ifeq ($(strip $(DEMO_TLS)),)
DEMO_TLS_MODE := off
DEMO_SCHEME := http
else ifeq ($(strip $(DEMO_TLS)),self)
# Inside the sandbox that is deleted at both ends of the run. `manage.py
# democert` writes both files, and nothing outlives the demo - which is the
# whole reason a pair that signs itself is an acceptable answer here. See that
# command for what the fingerprint it prints is for.
DEMO_TLS_MODE := self
DEMO_SCHEME := https
DEMO_CERT := $(DEMO_TLS_DIR)/demo.crt
DEMO_KEY := $(DEMO_TLS_DIR)/demo.key
else
# A path, so a certificate somebody else issued - for a name this box really
# answers to, from an authority a browser already trusts, which is the one way
# to show a demo without teaching its audience to click through a warning.
# Nothing is generated and nothing is deleted: the files belong to whoever
# pointed at them, they live outside the sandbox, and the sandbox going at the
# end of the run must not take them with it. Give DEMO_ADDRS the name on the
# certificate too, or the URLs printed will point somewhere it does not cover.
DEMO_TLS_MODE := given
DEMO_SCHEME := https
DEMO_CERT := $(strip $(DEMO_TLS))
DEMO_KEY := $(strip $(DEMO_TLS_KEY))
endif

ifeq ($(DEMO_SCHEME),https)
DEMO_TLS_ENV := AWG_PANEL_TLS=1 \
	AWG_PANEL_TLS_CERT=$(DEMO_CERT) \
	AWG_PANEL_TLS_KEY=$(DEMO_KEY)
else
DEMO_TLS_ENV :=
endif

# Prefer the panel's own virtualenv when it exists, so `make test` after
# `make venv` never picks up a stale system-wide Django.
PYTHON := $(shell if [ -x $(VENV)/bin/python ]; then echo $(VENV)/bin/python; else echo $(PY); fi)
RUFF := $(shell if [ -x $(VENV)/bin/ruff ]; then echo $(VENV)/bin/ruff; else echo ruff; fi)

# AWG_PANEL_ENV points at a file that does not exist: a laptop with a panel
# installed must not have /etc/awg-panel.env dragged into a dev run.
DEV_ENV ?= AWG_MOCK=1 \
	AWG_CONF_DIR=$(CURDIR)/$(DEV_DIR)/conf \
	AWG_PANEL_DATA=$(CURDIR)/$(DEV_DIR)/data \
	AWG_PANEL_ENV=$(CURDIR)/$(DEV_DIR)/panel.env \
	AWG_PANEL_BASE_PATH=/ \
	AWG_PANEL_DEBUG=1

# Every address this box answers on, as an origin on the demo port. Django's
# CSRF check compares the Origin a browser sent against this list, and a demo
# reached by IP from another machine is posting a login form from an origin the
# panel would otherwise have no reason to trust. The scheme comes from whether
# TLS was asked for, because an origin is compared scheme and all: a list built
# for https matches nothing on a plain run, and the login fails with a CSRF
# error rather than anything that names the cause.
COMMA := ,
EMPTY :=
SPACE := $(EMPTY) $(EMPTY)
DEMO_ADDRS := $(shell hostname -I 2>/dev/null) 127.0.0.1 localhost
DEMO_ORIGINS := $(subst $(SPACE),$(COMMA),$(strip \
	$(foreach addr,$(DEMO_ADDRS),$(DEMO_SCHEME)://$(addr):$(DEMO_PORT))))

# Which of those get printed as URLs to open. Derived from DEMO_ADDRS and not
# from a second `hostname -I`, so that overriding it moves the URLs, the CSRF
# origins and the certificate's names together. Apart is how this target tells
# its worst lie: `DEMO_ADDRS=dev.example.com` with a certificate issued for that
# name, printing an address URL the certificate does not cover, produces a
# name-mismatch warning that looks like a browser problem and is not.
#
# Loopback comes out because it is in DEMO_ADDRS for the certificate's sake and
# is no use in a link sent to somebody else. If taking it out empties the list,
# the run was loopback-only and the loopback URL is precisely what was wanted.
DEMO_URL_ADDRS := $(filter-out 127.0.0.1 ::1 localhost,$(DEMO_ADDRS))
ifeq ($(strip $(DEMO_URL_ADDRS)),)
DEMO_URL_ADDRS := $(DEMO_ADDRS)
endif

# The same sandbox as `make dev`, with one deliberate omission: AWG_PANEL_DEBUG
# is not set. `make dev` is loopback-only, so a traceback carrying source,
# local variables and settings values can only reach the person who started it.
# This target is reachable from other machines, and Django's debug page is the
# single most useful thing a panel could hand a stranger. WhiteNoise serves the
# built SPA either way, so nothing here needs DEBUG to work.
#
# AWG_PANEL_LISTEN and AWG_PANEL_PORT are how the address reaches gunicorn,
# which reads them out of deploy/gunicorn.conf.py. DEMO_TLS_ENV carries the
# certificate when there is one, and is empty when there is not: it is what
# makes the rest of the panel agree with the wire, so the cookies get Secure,
# HSTS is sent and every address the UI shows says https - all of which would
# lock a plain run out of its own login form if it were set unconditionally.
#
# The log level is turned down for one reason: gunicorn announces its start,
# its worker class and every worker it boots at info, and those five lines land
# under the block below saying what to open, which is the one thing somebody
# running this needs to be able to see. Anything gone wrong is a warning or an
# error and still prints.
DEMO_ENV ?= AWG_MOCK=1 \
	AWG_CONF_DIR=$(CURDIR)/$(DEMO_DIR)/conf \
	AWG_PANEL_DATA=$(CURDIR)/$(DEMO_DIR)/data \
	AWG_PANEL_ENV=$(CURDIR)/$(DEMO_DIR)/panel.env \
	AWG_PANEL_BASE_PATH=$(DEMO_BASE_PATH) \
	AWG_PANEL_TRUSTED_ORIGINS=$(DEMO_ORIGINS) \
	AWG_PANEL_LISTEN=$(DEMO_HOST) \
	AWG_PANEL_PORT=$(DEMO_PORT) \
	$(DEMO_TLS_ENV) \
	AWG_PANEL_LOG_LEVEL=warning

.PHONY: help venv lock wheels wallets dev demo build test lint fmt migrate collector

help: ## Show this list
	@printf 'AWG Panel - developer targets\n\n'
	@awk 'BEGIN { FS = ":.*## " } /^[a-z][a-z-]*:.*## / { printf "  %-10s %s\n", $$1, $$2 }' \
		$(MAKEFILE_LIST)
	@printf '\n'

venv: ## Create .venv from the locks (runtime plus the test and lint tools)
	$(PY) -m venv $(VENV)
	$(VENV)/bin/python -m pip install --quiet --require-hashes -r requirements-bootstrap.lock
	$(VENV)/bin/python -m pip install --quiet --require-hashes -r requirements-dev.lock

lock: ## Re-resolve the dependency locks from the ranges (network; commit the result)
	./lock-deps.sh

wheels: ## Download every wheel the locks name, so a bundle installs without PyPI
	./fetch-wheels.sh

# Prettier runs over the one file afterwards rather than the generator trying
# to guess where it would have broken a 95-character Monero address: the output
# is committed, and `npm run format:check` is part of lint.
wallets: frontend/node_modules ## Redraw the donation QR codes and the README's address list (commit the result)
	$(PYTHON) wallet-codes.py
	frontend/node_modules/.bin/prettier --write --print-width 100 \
		--log-level warn frontend/src/lib/walletCodes.ts

dev: $(DEV_DIR) frontend/node_modules ## Run the API on mock data and the Vite dev server
	@env $(DEV_ENV) $(PYTHON) manage.py migrate --noinput
	@printf '\n  api  http://127.0.0.1:$(API_PORT)/\n  ui   http://127.0.0.1:$(UI_PORT)/\n\n'
	@printf '  mock data, config tree in $(DEV_DIR)/conf - Ctrl-C stops both\n\n'
	@trap 'kill 0' INT TERM EXIT; \
		env $(DEV_ENV) $(PYTHON) manage.py runserver 127.0.0.1:$(API_PORT) & \
		API_PORT=$(API_PORT) npm --prefix frontend run dev -- --port $(UI_PORT) & \
		wait

# Nothing this target writes outlives it, which is why it can be pointed at a
# machine somebody else can reach: what it leaves behind would otherwise be a
# database holding an admin password hash and a config tree holding a private
# key per demo client, sitting in a checkout, belonging to nobody.
#
# Removed at both ends rather than only on the way out. The trap covers Ctrl-C,
# a closed terminal and a dropped SSH session; it cannot cover `kill -9` or the
# power going, so the next run clears whatever the last one left before it
# begins. That also makes every run identical, which is the property somebody
# reaching for a demo actually wants.
#
# HUP is named alongside INT and TERM, and it is the one that matters most here:
# closing a terminal is how somebody actually walks away from this, and a shell
# killed by an untrapped HUP does not run its EXIT trap. Without it the servers
# still died - the kernel signals the whole foreground group - and the sandbox
# was left sitting there, which is precisely the half that had to be cleaned up.
#
# The children are killed by pid rather than with `kill 0`, because `kill 0`
# signals the process group, which includes this shell: it could take the shell
# down before the line after it ran, leaving the same directory behind for the
# same reason.
#
# The guard is not ceremony. This expands to `rm -rf` on a path built from a
# variable, and `make demo DEMO_DIR=` would otherwise expand it to the panel
# directory itself.
#
# gunicorn rather than `manage.py runserver`, in every mode and not only the
# ones with a certificate. The development server speaks no TLS at all, so it
# could not serve two of the three; running it for the third would mean this
# target had two process models, two sets of flags and two ways to fail, and
# the one anybody debugging had used would be the other one. gunicorn is also
# the deployment shape install-panel.sh produces when there is no proxy in
# front, so what is being looked at is closer to what actually runs.
demo: frontend/node_modules ## Serve a seeded mock panel on one port; leaves nothing behind
	@test -n "$(strip $(DEMO_DIR))" || { echo "DEMO_DIR must not be empty" >&2; exit 1; }
ifeq ($(DEMO_TLS_MODE),given)
	@test -n "$(DEMO_KEY)" || { echo "DEMO_TLS names a certificate, so DEMO_TLS_KEY must name its key" >&2; exit 1; }
	@test -r "$(DEMO_CERT)" || { echo "cannot read the certificate: $(DEMO_CERT)" >&2; exit 1; }
	@test -r "$(DEMO_KEY)" || { echo "cannot read the key: $(DEMO_KEY)" >&2; exit 1; }
endif
	@rm -rf "$(CURDIR)/$(DEMO_DIR)"
	@mkdir -p "$(CURDIR)/$(DEMO_DIR)"
ifeq ($(DEMO_TLS_MODE),self)
	@mkdir -p "$(DEMO_TLS_DIR)"
	@chmod 700 "$(DEMO_TLS_DIR)"
endif
	@env $(DEMO_ENV) $(PYTHON) manage.py migrate --noinput >/dev/null
	@npm --prefix frontend run build --silent
	@env $(DEMO_ENV) $(PYTHON) manage.py collectstatic --noinput >/dev/null
	@env $(DEMO_ENV) $(PYTHON) manage.py seeddemo
ifeq ($(DEMO_TLS_MODE),self)
	@env $(DEMO_ENV) $(PYTHON) manage.py democert \
		--cert "$(DEMO_CERT)" --key "$(DEMO_KEY)" \
		$(foreach addr,$(DEMO_ADDRS),--host $(addr))
endif
ifeq ($(DEMO_TLS_MODE),given)
	@printf '  cert         %s\n' "$(DEMO_CERT)"
endif
	@first=1; \
		for addr in $(DEMO_URL_ADDRS); do \
			if [ $$first = 1 ]; then printf '  open         '; first=0; \
			else printf '               '; fi; \
			printf '$(DEMO_SCHEME)://%s:$(DEMO_PORT)$(DEMO_BASE_PATH)\n' "$$addr"; \
		done; \
		printf '\n'
	@printf '  Ctrl-C stops the panel and the collector and deletes $(DEMO_DIR)\n'
ifeq ($(DEMO_TLS_MODE),off)
	@printf '  no TLS: the password above, the session behind it and a private key\n'
	@printf '  per client all cross the network in the clear\n'
	@printf '  DEMO_TLS=self serves a certificate made for this run alone;\n'
	@printf '  DEMO_TLS=<cert> DEMO_TLS_KEY=<key> serves one you already have\n'
endif
ifeq ($(DEMO_TLS_MODE),self)
	@printf '  the browser warns once: the certificate signs itself, and the\n'
	@printf '  fingerprint above is the one it should be showing you\n'
endif
ifneq ($(DEMO_BASE_PATH),/)
	@printf '  the path is the secret - anything outside it is an empty 404\n'
endif
	@printf '  reachable from any machine that can route here; not for a public interface\n\n'
	@pids=""; \
		trap 'kill $$pids 2>/dev/null; rm -rf "$(CURDIR)/$(DEMO_DIR)"' INT TERM HUP EXIT; \
		env $(DEMO_ENV) $(PYTHON) manage.py collector >/dev/null 2>&1 & \
		pids="$$!"; \
		env $(DEMO_ENV) $(PYTHON) -m gunicorn --config deploy/gunicorn.conf.py awgui.wsgi:application & \
		pids="$$pids $$!"; \
		wait

build: frontend/node_modules ## Build the SPA and collect the static files
	npm --prefix frontend run build
	env $(DEV_ENV) $(PYTHON) manage.py collectstatic --noinput

test: ## Run the test suite
	env $(DEV_ENV) $(PYTHON) -m pytest $(PYTEST_ARGS)

lint: ## Check formatting and lint rules, changing nothing
	$(RUFF) check .
	$(RUFF) format --check .

fmt: ## Format the Python sources and apply the safe lint fixes
	$(RUFF) format .
	$(RUFF) check --fix .

migrate: $(DEV_DIR) ## Apply database migrations
	env $(DEV_ENV) $(PYTHON) manage.py migrate --noinput

collector: $(DEV_DIR) ## Run the traffic collector in the foreground
	env $(DEV_ENV) $(PYTHON) manage.py collector

$(DEV_DIR):
	@mkdir -p $(DEV_DIR)/conf $(DEV_DIR)/data
	@chmod 700 $(DEV_DIR)/conf $(DEV_DIR)/data

# npm ci and nothing else: the "|| npm install" that used to be here rewrote
# package-lock.json whenever ci was unhappy, so the fix for a lockfile problem
# was to silently stop having a lockfile. If this fails, the lock and
# package.json genuinely disagree - run `npm --prefix frontend install`
# yourself, look at what it changed, and commit that.
frontend/node_modules: frontend/package.json frontend/package-lock.json
	npm --prefix frontend ci --no-audit --no-fund
	@touch $@
