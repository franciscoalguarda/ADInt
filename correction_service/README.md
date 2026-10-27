# Correction Service

Serviço Flask da Parte 1 para carregar digitalizações de exames, atribuir
professores às questões e guardar notas e comentários. Usa SQLAlchemy e uma
base de dados SQLite própria, criada automaticamente em `corrections.sqlite`.

Nesta parte, todas as páginas são públicas, sem filtros por utilizador nem
ligação aos restantes serviços. A consulta de correções não identifica alunos.

## Executar

Na raiz do projeto, em PowerShell, com o ambiente virtual `venv`:

```powershell
.\venv\Scripts\python.exe -m pip install -r correction_service\requirements.txt
.\venv\Scripts\python.exe correction_service\cs_app.py
```

Abrir <http://127.0.0.1:5004>. O servidor inicia em modo de desenvolvimento.

## Páginas e endpoints

| URL | Métodos | Função |
| --- | --- | --- |
| `/` | GET | Resumo dos PDFs e correções. |
| `/assignments` | GET, POST | AD_4: listar e atribuir professores a questões. |
| `/scans/upload` | GET, POST | AD_5: carregar e processar um PDF. |
| `/answers` | GET | PR_1: listar respostas por corrigir. |
| `/answers/<id>/correct` | GET, POST | PR_2: visualizar a resposta e guardar ou editar a correção. |
| `/answers/corrected` | GET | PR_3: listar respostas corrigidas. |
| `/scans` | GET | ST_3 parcial: consultar digitalizações e correções. |
| `/answers/<id>/page.pdf` | GET | Consultar a página processada em PDF. |
| `/answers/<id>/page.png` | GET | Consultar a imagem processada. |
| `/scans/<id>/original.pdf` | GET | Descarregar o PDF original. |

Os identificadores de exame e professor são introduzidos manualmente. Cada
par exame/questão tem uma atribuição; guardar novamente atualiza o professor.
As respostas sem professor atribuído também podem ser corrigidas.

## Processamento e correção

- Cada folha tem quatro questões, pela ordem 1–4. Os PDFs produzidos pelo
  Printing Service contêm exatamente `4 × n` páginas. Também se aceitam os
  exemplos de cinco páginas do ZIP, ignorando a primeira página introdutória
  (`4 × n + 1`). Outros totais são rejeitados.
- O PDF original é preservado. As páginas usadas para correção são guardadas
  como PDF e PNG, com os QR codes detetados cobertos a cinzento.
- A nota deve ser finita e não negativa, sem máximo definido. O comentário é
  opcional. Guardar uma nota, incluindo zero, marca a resposta como corrigida.
  Não se calcula a nota final do exame.

O limite de upload é 50 MB. PDFs vazios, inválidos ou protegidos por password
são rejeitados. O upload é guardado numa única transação após o processamento.

## Estrutura e dados

| Ficheiro | Conteúdo |
| --- | --- |
| `cs_app.py` | Configuração, rotas, validação e processamento dos PDFs. |
| `correction_data.py` | Modelos `ProfessorAssignment`, `ExamCorrectionPdf` e `QuestionCorrection`: atribuições, PDFs originais, páginas e correções. |
| `process_student_answers.py` | Script original do ZIP, utilizado sem alterações. |
| `templates/` | Formulários, listagens e visualização das respostas. |

`CORRECTION_DATABASE_URL` permite alterar a ligação à base de dados e
`CORRECTION_SECRET_KEY` definir a chave das sessões Flask. Por omissão,
usa-se o SQLite na pasta do serviço e uma chave aleatória por arranque.
