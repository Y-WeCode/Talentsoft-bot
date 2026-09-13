# Talentsoft-bot : helpers ops. Doc intégrée : make help

.DEFAULT_GOAL := help

COMPOSE ?= docker compose
PROFILE ?= --profile async
API     ?= talentsoft_bot_api
WORKER  ?= talentsoft_bot_worker
BASE    ?= http://127.0.0.1:42201

.PHONY: help up down deploy deploy-nocache restart ps health worker-status verify-code logs \
        logs-worker traces shell-api test lint format selftest event job reset-session discover \
        check-async-env

help:
	@echo "Talentsoft-bot : ops helper"
	@echo ""
	@echo "Cycle de vie"
	@echo "  make up              Démarrage (api seule, mode sync)"
	@echo "  make deploy          Rebuild api + worker + redis (mode async)"
	@echo "  make deploy-nocache  Build --no-cache puis up"
	@echo "  make restart / ps / down"
	@echo ""
	@echo "Vérifs"
	@echo "  make health          GET / sur $(BASE)"
	@echo "  make worker-status   Propriétaire du navigateur, état du worker, profondeur des files"
	@echo "  make selftest        POST /selftest (login + candidature témoin + sélecteurs critiques)"
	@echo "  make verify-code     Même version de code dans api ET worker (piège image stale)"
	@echo "  make logs / logs-worker / traces / shell-api"
	@echo ""
	@echo "Développement"
	@echo "  make test            ruff + pytest (unitaires + bout en bout sur faux Back Office)"
	@echo "  make lint / format"
	@echo "  make discover URL=https://<tenant>.talent-soft.com/... Capture headless (login + dump DOM)"
	@echo ""
	@echo "API de test (token lu depuis .env, jamais affiché)"
	@echo "  make event EMAIL=<email> OFFER=<offer_id> COMMENT='Test Hippolyte.ai' [TYPE='...'] [DOC=fichier.pdf]"
	@echo "  make job ID=<job_id>"
	@echo "  make reset-session   Sortie de l'état dégradé"

up:
	$(COMPOSE) up -d --build api

down:
	$(COMPOSE) $(PROFILE) down

# Le worker est le SEUL a piloter un navigateur. Si l'api ignore ce reglage, elle ouvre le
# sien en plus : deux sessions sur le meme compte technique, qui se deconnectent mutuellement.
# On refuse le deploiement plutot que de laisser la panne s'installer.
check-async-env:
	@grep -qE '^TS_ASYNC_JOBS_ENABLED=true$$' .env \
	  || { echo "ERREUR: .env doit contenir TS_ASYNC_JOBS_ENABLED=true (l'api DOIT deleguer au worker)"; exit 1; }
	@grep -qE '^REDIS_URL=.+' .env \
	  || { echo "ERREUR: .env doit contenir REDIS_URL (ex. redis://talentsoft_bot_redis:6379/0)"; exit 1; }

deploy: check-async-env
	$(COMPOSE) $(PROFILE) up -d --build api worker redis

deploy-nocache: check-async-env
	$(COMPOSE) $(PROFILE) build --no-cache api worker
	$(COMPOSE) $(PROFILE) up -d api worker redis

restart:
	$(COMPOSE) restart api
	-$(COMPOSE) $(PROFILE) restart worker

ps:
	$(COMPOSE) $(PROFILE) ps -a

health:
	curl -m 5 $(BASE)/ ; echo

worker-status:
	@echo "Proprietaire du navigateur et etat du worker (aucune ouverture de navigateur) :"
	@curl -sS -m 5 $(BASE)/ | python -c "import json,sys; d=json.load(sys.stdin); print(json.dumps({k: d.get(k) for k in ('browser_owner','worker','queues','login_count','degraded','degraded_reason')}, indent=2, ensure_ascii=False))"

verify-code:
	@for c in $(API) $(WORKER); do \
	  echo "== $$c =="; \
	  docker exec $$c sh -c 'grep -c "def update_application" /app/app/scraper.py' 2>/dev/null || echo "conteneur absent"; \
	  docker inspect $$c --format 'Created={{.Created}} Image={{.Image}}' 2>/dev/null || true; \
	done

logs:
	docker logs -f --since 10m $(API)

logs-worker:
	docker logs -f --since 10m $(WORKER)

traces:
	ls -lt traces 2>/dev/null | head || echo "pas de traces/"

shell-api:
	docker exec -it $(API) /bin/sh

lint:
	ruff check . && ruff format --check .

format:
	ruff format . && ruff check --fix .

test: lint
	pytest -q

selftest:
	@TOKEN=$$(grep '^API_TOKEN=' .env | cut -d= -f2- | sed 's/^["'\'']//;s/["'\'']$$//') ; \
	curl -sS -X POST "$(BASE)/selftest" -H "Authorization: Bearer $$TOKEN" ; echo

reset-session:
	@TOKEN=$$(grep '^API_TOKEN=' .env | cut -d= -f2- | sed 's/^["'\'']//;s/["'\'']$$//') ; \
	curl -sS -X POST "$(BASE)/admin/reset-session" -H "Authorization: Bearer $$TOKEN" ; echo

# make event EMAIL=candidat@x.fr OFFER=25152 COMMENT='Test' [TYPE='...'] [DOC=synthese.pdf]
event:
ifndef EMAIL
	$(error Usage: make event EMAIL=<email> OFFER=<offer_id> COMMENT='...' [TYPE='...'] [DOC=fichier.pdf])
endif
ifndef OFFER
	$(error Usage: make event EMAIL=<email> OFFER=<offer_id> COMMENT='...' [TYPE='...'] [DOC=fichier.pdf])
endif
	@TOKEN=$$(grep '^API_TOKEN=' .env | cut -d= -f2- | sed 's/^["'\'']//;s/["'\'']$$//') ; \
	curl -sS -X POST "$(BASE)/update-application" \
	  -H "Authorization: Bearer $$TOKEN" \
	  -F "candidate_email=$(EMAIL)" \
	  -F "offer_id=$(OFFER)" \
	  $(if $(TYPE),-F "event_type=$(TYPE)",) \
	  $(if $(COMMENT),-F "comment=$(COMMENT)",) \
	  $(if $(DOC),-F "documents=@$(DOC)",) ; echo

job:
ifndef ID
	$(error Usage: make job ID=<job_id>)
endif
	@TOKEN=$$(grep '^API_TOKEN=' .env | cut -d= -f2- | sed 's/^["'\'']//;s/["'\'']$$//') ; \
	curl -sS "$(BASE)/jobs/$(ID)" -H "Authorization: Bearer $$TOKEN" ; echo

discover:
	python tools/discover.py --out discovery --login --dump $(if $(URL),--open "$(URL)",)
