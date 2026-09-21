# Documentação do Backend — KANdo

## Escopo e autoria

A camada de Backend do KANdo foi concebida e desenvolvida por Karina [![GitHub](https://img.shields.io/badge/GitHub-KarinaS0uza-181717?style=flat&logo=github)](https://github.com/KarinaS0uza)

## Visão geral

O backend do KANdo é uma API Django REST responsável por autenticação, persistência, processamento de currículos e vagas, orquestração dos módulos de IA, matching, avaliação técnica, dashboard, trilha e Talent Passport.

## Stack

- Python e Django 6 (versão instalada: 6.0.7)
- Django REST Framework (3.17.1)
- JWT (`djangorestframework-simplejwt`, 5.5.1)
- `django-cors-headers` (4.9.0)
- `pdfplumber` e `pypdfium2` para extração e processamento de PDFs
- Groq para chamadas ao modelo de IA (SDK `groq`, 1.5.0 — único provider de IA do projeto)
- SQLite para desenvolvimento local
- PostgreSQL/Supabase opcional para banco remoto (via `psycopg`)

### Substituição do Docling pelo pdfplumber

A extração de texto dos PDFs foi inicialmente implementada com o Docling.
Entretanto, a biblioteca e suas dependências consumiam mais memória do que o
disponível no ambiente de deploy do Render, causando falhas durante a construção
ou execução da aplicação.

Para viabilizar o deploy, o Docling foi substituído pelo `pdfplumber`, uma
solução mais leve e suficiente para extrair o texto de currículos e vagas em
PDF. A mudança reduziu o consumo de memória e o tamanho das dependências,
permitindo que o backend fosse executado dentro dos limites do Render.

Após a substituição, também foram adicionados tratamentos para PDFs corrompidos,
protegidos por senha, sem texto utilizável e para falhas no armazenamento
temporário.

## Apps

| App | Responsabilidade |
|---|---|
| `users` | usuário customizado, cadastro, login e CRUD |
| `resumes` | envio, extração e normalização de currículos |
| `jobs` | envio, extração e normalização de vagas |
| `matching` | comparação entre currículo e vaga |
| `assessments` | geração, respostas e avaliação do simulado |
| `passports` | dashboard, trilha, Passport e autoavaliação |
| `ai_core` | prompts, chamadas à IA, metadados e contratos JSON |
| `config` | configurações e roteamento global |

> Além dos apps acima, `local_scripts/` guarda scripts locais fora do controle de versão
> (gitignored), incluindo `seed_prompts.py` e `prompts_data.py`, usados para popular os
> prompts de IA no banco (ver `docs/ai.md`).

## Autenticação

O backend usa JWT. Salvo cadastro e login, as rotas exigem autenticação.

```http
Authorization: Bearer <access_token>
```

O access token tem validade de 2 horas e o refresh token de 7 dias
(configurado em `SIMPLE_JWT`, em `config/settings.py`).

Rotas públicas:

| Método | Rota | Finalidade |
|---|---|---|
| `POST` | `/api/users/create/` | Cadastro |
| `POST` | `/api/login/` | Retorna access token, refresh token e `user_id` |

## Configuração

As configurações são carregadas de `backend/.env`. Exemplo da estrutura atual
do arquivo `.env`:

```env
GROQ_API_KEYS=...

DJANGO_SECRET_KEY=...
DJANGO_DEBUG=True
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
DJANGO_CORS_ALLOW_ALL_ORIGINS=True
DJANGO_CORS_ALLOWED_ORIGINS=

PORT=8000

# DATABASE_ENGINE=sqlite (padrão)
DATABASE_ENGINE=supabase
DB_NAME=postgres
DB_USER=...
DB_PASSWORD=...
DB_HOST=...
DB_PORT=5432
```

A API usa SQLite quando `DATABASE_ENGINE` não é informado. Na configuração
atual, `DATABASE_ENGINE=supabase` habilita PostgreSQL/Supabase por meio das
variáveis `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST` e `DB_PORT`.

`GROQ_API_KEYS` recebe uma ou mais chaves separadas por vírgula. O backend pode
alternar entre elas quando uma chave atinge o limite de uso ou é rejeitada.
Os valores sensíveis foram omitidos do exemplo e não devem ser versionados.

`DEBUG`, `ALLOWED_HOSTS` e as variáveis de CORS são sempre lidas do ambiente
(`os.environ.get(...)`) — nunca hardcoded em `settings.py`.

> **Atenção:** `DJANGO_DEBUG=True` e `DJANGO_CORS_ALLOW_ALL_ORIGINS=True` são
> adequados apenas para desenvolvimento. Em produção, ambos devem ser definidos
> como `False`, e `DJANGO_CORS_ALLOWED_ORIGINS` deve listar explicitamente as
> origens autorizadas.

## Endpoints

### Infraestrutura

| Método | Rota | Finalidade |
|---|---|---|
| `GET` | `/health/` | Health check público para monitoramento e deploy |
| `*` | `/admin/` | Admin padrão do Django |

### Usuários

| Método | Rota |
|---|---|
| `GET` | `/api/users/` |
| `GET` | `/api/users/<uuid>/` |
| `PUT/PATCH` | `/api/users/<uuid>/update/` |
| `DELETE` | `/api/users/<uuid>/delete/` |

### Currículos e vagas

| Método | Rota | Corpo principal |
|---|---|---|
| `GET, POST` | `/api/resumes/` | texto bruto ou PDF |
| `GET, DELETE` | `/api/resumes/<uuid>/` | Consulta ou remove um currículo |
| `GET, POST` | `/api/job-postings/` | texto bruto ou PDF |
| `GET, DELETE` | `/api/job-postings/<uuid>/` | Consulta ou remove uma vaga |

As submissões são normalizadas. Em caso de falha da IA, o backend registra o estado de erro sem derrubar toda a aplicação.

### Matching

| Método | Rota | Corpo |
|---|---|---|
| `GET, POST` | `/api/matching/` | `{ "resume_id": "...", "job_id": "..." }` |
| `GET, DELETE` | `/api/matching/<uuid>/` | Consulta ou remove um matching |

O resultado inclui score geral, `matches`, `gaps`, forças e pontos de melhoria.

### Simulado

| Método | Rota | Corpo |
|---|---|---|
| `GET, POST` | `/api/assessments/` | `{ "resume_id": "...", "job_id": "..." }` |
| `GET, DELETE` | `/api/assessments/<uuid>/` | — |
| `POST` | `/api/assessments/<uuid>/result/` | `{ "answers": [{ "id": "B1Q1", "answer": "..." }] }` |

O resultado do simulado contém avaliação por pergunta, score agregado e dados por skill.

### Talent Passport

| Método | Rota | Corpo |
|---|---|---|
| `GET, POST` | `/api/passports/` | `{ "resume_id": "...", "job_id": "..." }` |
| `GET` | `/api/passports/<uuid>/` | — |
| `GET, POST` | `/api/passports/waiting-readiness/` | autoavaliação de preparo |

Pré-requisitos para gerar um Passport: currículo e vaga normalizados, matching concluído, simulado gerado e respostas corrigidas. Um Passport é único por par currículo/vaga; gerar novamente atualiza o existente.

> Ao contrário dos outros recursos, `/api/passports/<uuid>/` não tem `DELETE` implementado.

### Prompts de IA

| Método | Rota | Finalidade |
|---|---|---|
| `GET` | `/api/prompts/` | Lista prompts |
| `POST` | `/api/prompts/create/` | Cria prompt |
| `GET` | `/api/prompts/<uuid>/` | Consulta prompt |
| `PUT/PATCH` | `/api/prompts/<uuid>/update/` | Atualiza prompt |

## Migrations

As migrations são gitignoradas (`**/migrations/` no `.gitignore`) — cada ambiente precisa
gerá-las localmente (`python manage.py makemigrations`) antes de rodar `migrate` pela
primeira vez. Apenas 4 arquivos `__init__.py` de migrations (`jobs`, `resumes`, `users`,
`passports`) ficaram rastreados no git; `matching`, `assessments` e `ai_core` não têm
nada rastreado.

## Estado do código

Nenhuma ocorrência de `TODO`/`FIXME`/`XXX`/`HACK` em nenhum arquivo `.py` do backend
(verificado via grep repo-wide, excluindo venv e migrations).

## Fluxo de dados

> Seção reconstruída a partir dos pré-requisitos e dependências já descritos acima
> (ex.: pré-requisitos do Talent Passport) — vale revisar contra o diagrama de arquitetura
> do `README.md` para confirmar se bate exatamente com o desenho oficial do fluxo.

1. **Cadastro e login** (`/api/users/create/`, `/api/login/`) — usuário obtém par de tokens JWT.
2. **Envio de currículo e vaga** (`/api/resumes/`, `/api/job-postings/`) — texto ou PDF é
   normalizado via IA (`resume_normalization`, `job_normalization`) e persistido em formato
   estruturado.
3. **Matching** (`/api/matching/`) — currículo e vaga normalizados são comparados via IA
   (`job_resume_matching_analysis`), gerando score, skills compatíveis/faltantes, forças e
   pontos de melhoria.
4. **Simulado técnico** (`/api/assessments/`) — perguntas são geradas via IA
   (`question_generation`) a partir do currículo e da vaga; respostas do candidato são
   avaliadas em `/api/assessments/<uuid>/result/` via IA (`question_answer`).
5. **Agregação** — as avaliações individuais do simulado são consolidadas por skill em
   código (não é uma etapa de IA).
6. **Talent Passport** (`/api/passports/`) — a partir de currículo, matching e avaliação já
   concluídos, a IA gera perfil (`talent_passport_profile`) e trilha de estudo
   (`talent_passport_study_track`); o dashboard é calculado deterministicamente em código.