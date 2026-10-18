# Documentação da IA — Kando / Talent Passport

## Escopo e autoria

A camada de IA do Kando foi concebida e desenvolvida por  Andreia [![GitHub](https://img.shields.io/badge/GitHub-Deialima-181717?style=flat&logo=github)](https://github.com/Deialima): prompts, contratos JSON, regras de negócio, validações e testes locais. O backend Django realiza sua integração ao produto, oferecendo persistência, endpoints, autenticação e orquestração.

Esta documentação descreve a camada de IA como uma área própria do produto; ela não é apenas um detalhe interno do backend.

## Objetivo

A IA transforma currículo, vaga e respostas do candidato em informações úteis para preparação profissional:

1. estrutura currículo e vaga;
2. identifica compatibilidade e lacunas;
3. gera perguntas técnicas personalizadas;
4. avalia respostas com feedback;
5. consolida desempenho;
6. produz perfil profissional, recomendações e trilha de estudo.

## Princípio central

> IA é usada para julgamento qualitativo; código é usado para cálculo e regras determinísticas.

### A IA é responsável por

- extração semântica de currículos e vagas;
- interpretação de requisitos;
- comparação qualitativa entre perfil e vaga;
- geração de perguntas;
- avaliação textual de respostas;
- síntese de perfil e recomendações.

### O código é responsável por

- médias e scores;
- faixas de senioridade e proficiência;
- agregação por skill;
- limiares e regras de desbloqueio;
- correspondências de nome controladas;
- persistência, autenticação e validação de propriedade.

## Módulos

| Módulo | `PROMPT_KEY` | Entrada | Saída principal |
|---|---|---|---|
| Normalização de currículo | `resume_normalization` | texto/PDF do currículo | candidato, skills, experiências, formação e certificações |
| Normalização de vaga | `job_normalization` | texto/PDF da vaga | requisitos, tecnologias, senioridade, área e elegibilidade |
| Matching | `job_resume_matching_analysis` | currículo e vaga estruturados | score, skills compatíveis/faltantes, forças e melhorias |
| Geração de perguntas | `question_generation` | currículo e vaga estruturados | desafio em blocos e perguntas conceituais |
| Avaliação de respostas | `question_answer` | pergunta, critérios, senioridade e resposta | score, skills, evidências e feedback |
| Agregação | — (não é módulo de IA) | avaliações individuais | score geral e desempenho por skill |
| Perfil profissional | `talent_passport_profile` | currículo, matching e avaliação | resumo, nível, competências e recomendações |
| Trilha de estudo | `talent_passport_study_track` | lacunas e desempenho | passos priorizados e recursos sugeridos |

> A coluna `PROMPT_KEY` identifica a chave usada no código para buscar o prompt ativo
> correspondente (ver seção "Armazenamento de prompts"). "Agregação" está marcada
> explicitamente como não sendo um módulo de IA — é cálculo determinístico em Python
> (`passports/services/dashboard.py`), coerente com o princípio central acima.

## Regras de negócio importantes

### Currículo

- Experiência técnica e senioridade são calculadas em código a partir dos dados estruturados.
- A IA pode extrair experiências e skills, mas não deve ser fonte final de cálculo numérico.

### Matching

- Distinguir sinônimos reais de relação geral ↔ específica.
- Exemplo: SQL e PostgreSQL não são equivalentes absolutos. Uma vaga que pede SQL pode ser compatível com experiência em PostgreSQL; o inverso pode representar lacuna parcial.
- Requisitos de elegibilidade merecem prioridade máxima quando não atendidos.

### Perguntas

- O desafio é conceitual; não deve exigir código.
- Tecnologias existentes somente na vaga devem gerar perguntas de familiaridade/consciência, não perguntas profundas de arquitetura ou trade-offs.
- Perguntas devem ser vinculadas a skills avaliadas e identificadas de forma estável.

### Avaliação

- Resposta vazia recebe score zero sem chamada à IA.
- As avaliações individuais são consolidadas por skill em código.
- O resultado por skill deve ser reutilizado por dashboard e trilha, evitando médias duplicadas.

### Talent Passport

- `proficiency_level` é calculado em código usando o nível de confiança da skill do currículo.
- Correspondência entre a skill do perfil e a skill do currículo usa match exato, depois case-insensitive.
- Se não houver correspondência, existe fallback explícito com log e indicação de confiança; não deve haver fallback silencioso.

## Contratos JSON

As respostas são solicitadas como JSON estruturado. O projeto possui canonicalização de chaves e valores para normalizar formatos legados ou em português para o contrato interno em inglês.

Exemplos de campos canônicos:

```text
technical_skills
required_skills
matching_skills
missing_skills
```

## Camada de abstração

Todo o acesso ao provedor de LLM passa por uma camada única, em `ai_core/llm.py`, que expõe
`run_prompt` e `run_prompt_safe`. Os 7 módulos de negócio (tabela acima) chamam
exclusivamente `run_prompt_safe` — nenhum deles fala diretamente com o SDK do provedor.

O SDK `openai`, configurado para chamar o OpenRouter, é importado apenas em
`ai_core/llm.py` (e em seu teste correspondente), o que
mantém a troca de provedor, modelo ou estratégia de retry centralizada num único ponto do
código.

## Armazenamento de prompts

Cada etapa depende de um prompt ativo, armazenado na tabela `ai_core.Prompt` — versionada,
com uma versão ativa por `prompt_description` (auditoria de chamadas em
`PromptCallMetadata`).

A carga inicial é feita por `local_scripts/seed_prompts.py`, a partir do conteúdo definido
em `local_scripts/prompts_data.py` (ambos fora do controle de versão). As 7 chaves
`PROMPT_KEY` usadas no código batem exatamente com os 7 `prompt_description` seedados —
não há inconsistência entre o que o código pede e o que é populado no banco.

> **Não verificado:** se o conteúdo atualmente ativo no banco (produção/local) é idêntico ao
> definido em `prompts_data.py`. Como os prompts podem ser atualizados via
> `/api/prompts/create/` e `/api/prompts/<uuid>/update/` diretamente, o arquivo fonte local
> não é necessariamente a versão vigente — vale confirmar consultando o banco quando isso
> for relevante para uma investigação.

## Operação da camada de IA

A camada de IA usa a API do OpenRouter em `https://openrouter.ai/api/v1`, por
meio do SDK compatível `openai` (3.16.2). A configuração aceita uma chave única
em `OPENROUTER_API_KEY` ou múltiplas chaves, separadas por vírgula, em
`OPENROUTER_API_KEYS`. Quando ambas estão presentes, a lista de chaves tem
precedência; entradas vazias são descartadas. As variáveis antigas `GROQ_*`
não são mais lidas. Uma chave do OpenRouter é necessária para inicializar a
camada de IA; credenciais de outra API não são usadas como fallback.

```env
OPENROUTER_API_KEY=sua_chave_aqui
# OPENROUTER_API_KEYS=chave_1,chave_2
OPENROUTER_MODEL=dots-studio/dots-3-note-preview:free
```

`dots-studio/dots-3-note-preview:free` é o padrão caso `OPENROUTER_MODEL` não
seja definida. Em 18/10/2026, o OpenRouter lista a retirada desse preview para
31/12/2026; mantenha o modelo configurável e confirme sua disponibilidade antes
do deploy ([página do modelo](https://openrouter.ai/dots-studio/dots-3-note-preview:free)).

Os prompts ativos, os contratos JSON, a temperatura padrão (`0.2`) e as
mensagens de erro em português foram preservados. As chamadas continuam
síncronas, sem streaming, com `response_format={"type": "json_object"}` e
`provider.require_parameters=true` para exigir suporte aos parâmetros enviados.
O raciocínio usa o padrão do modelo; não há ajuste automático de prompts,
reparo de JSON ou fallback para outro modelo.

O timeout do cliente permanece em 5 segundos para conexão e 60 segundos para
leitura/escrita/pool. A geração de perguntas mantém seu override de 60 segundos
e a avaliação de cada resposta, 30 segundos. Esses limites se aplicam a cada
tentativa, não ao tempo total incluindo retries.

Cada etapa depende de um prompt ativo no banco. Falhas como prompt ausente,
JSON inválido, limite de uso ou indisponibilidade do provedor são retornadas
de modo controlado e persistidas no fluxo correspondente, sem encerrar a
aplicação.

### Retry e rate limit (detalhamento)

O tratamento de falhas transitórias é centralizado em `create_completion`
(`ai_core/llm.py`), e não implementado individualmente por módulo:

- Em rate limit, o tempo de espera é calculado a partir do header/mensagem retornado pelo
  próprio erro do provedor, com teto de 10 segundos e padrão de 2 segundos.
- É feita uma retentativa usando a mesma chave de API.
- Se ainda assim falhar, o sistema faz fallback para a próxima chave configurada em
  `OPENROUTER_API_KEYS`; falhas de autenticação/permissão também permitem essa troca.
- Em erro `400` do tipo "failed to validate json" (JSON malformado retornado pelo próprio
  modelo), também é feita uma retentativa antes de propagar a falha.
- As duas retentativas automáticas do SDK são preservadas, além das retentativas
  acima. Por exemplo, um rate limit HTTP persistente pode resultar em seis
  tentativas HTTP por chave (três em cada chamada do wrapper).
- Erros `402` por falta de crédito são falhas de configuração não retentáveis.
  Reservas temporárias (`metadata.limit_source=openrouter_in_flight_budget` ou
  header `Retry-After`) continuam retentáveis pelo fluxo chamador, sem uma nova
  política de retry imediato ou troca de chave para esse caso.
- Erros embutidos no corpo de respostas HTTP `200`, inclusive junto de conteúdo
  parcial, são classificados como falhas antes de aceitar o JSON como sucesso.
  Cada chamada lógica continua produzindo uma única linha de `PromptCallMetadata`.

### Validação da migração e deploy

Os testes automatizados usam SQLite e transporte HTTP simulado, sem chamar a
API real. A chave abaixo é fictícia e serve apenas para inicializar o cliente:

```bash
cd backend
DATABASE_ENGINE=sqlite OPENROUTER_API_KEY=test-only OPENROUTER_API_KEYS=test-only \
  python -m pytest
```

Antes de publicar no Render, configure uma chave real do OpenRouter no ambiente
local ou de homologação e valide os sete fluxos com entradas representativas e
os mesmos prompts ativos: currículo, vaga, matching, geração de perguntas,
avaliação de respostas, perfil do Passport e narrativa da trilha de estudo.
Confira o JSON esperado em cada fluxo, texto em português, intervalos dos scores,
cálculos determinísticos e conclusão dentro dos timeouts atuais. Inclua entrada
inválida, resposta em branco e currículo/vaga com lacunas. A troca de modelo pode
alterar conteúdo e latência, mesmo com os contratos preservados.

Publique somente após essa validação e confira status, tokens e duração em
`PromptCallMetadata`. Em caso de falha, restaure a revisão anterior com suas
dependências e variáveis `GROQ_*`. Não é necessária migração de banco, e os
prompts existentes não devem ser reescritos como parte desta troca.

## Limitações conhecidas

> **Nota — histórico da escolha de modelo:** durante a fase de testes, a equipe avaliou
> múltiplos modelos servidos pela Groq (incluindo Llama, GPT e Qwen) ao longo de
> aproximadamente uma semana. O Llama apresentou os resultados mais consistentes nos
> testes locais e foi a escolha inicial para produção. Dois dias antes da entrega do
> projeto, a Groq comunicou por e-mail a descontinuação do modelo Llama utilizado,
> exigindo a migração para o segundo modelo com melhor desempenho nos testes
> (`openai/gpt-oss-120b`) em um prazo de 24 horas. Essa migração emergencial explica
> as trocas de modelo visíveis no histórico de commits (qwen → gpt → qwen → gpt) e
> reforça a recomendação acima de sempre conferir `ai_core/llm.py` e a variável
> `OPENROUTER_MODEL` efetivamente configurada em produção (antes da migração, `GROQ_MODEL`), em vez de assumir que o modelo
> documentado aqui é definitivo — dado o precedente de descontinuações por parte do
> provedor com pouco aviso prévio.

- **Sem validação contra o texto de entrada:** a canonicalização de contratos JSON
  (`ai_core/json_contracts.canonicalize_json`) traduz chaves e valores de PT para EN, mas
  não confere se o que a IA extraiu de fato aparece no texto original enviado (currículo ou
  vaga). O cálculo determinístico de `technical_experience_years`/`calculated_seniority`
  (`resumes/services/experience_calculation.py`) também parte da lista de experiências já
  extraída pela IA, sem checagem contra o texto bruto. Vale considerar como um risco de
  qualidade a monitorar, especialmente em casos de alucinação do modelo.
