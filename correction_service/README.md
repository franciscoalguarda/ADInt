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
| `/` | GET | Cartões de resumo e de ação, respostas pendentes e progresso da correção. |
| `/assignments` | GET, POST | AD_4: listar e atribuir professores a questões. |
| `/assignments/success` | GET | Confirmar a atribuição e apresentar a listagem de professores. |
| `/scans/upload` | GET, POST | AD_5: carregar e processar um PDF. |
| `/answers` | GET | PR_1: listar respostas por corrigir. |
| `/answers/<id>/correct` | GET, POST | PR_2: visualizar a resposta e guardar ou editar a correção. |
| `/answers/corrected` | GET | PR_3: listar respostas corrigidas. |
| `/answers/corrected/success` | GET | Confirmar a correção e apresentar as respostas corrigidas. |
| `/scans` | GET | ST_3 parcial: consultar digitalizações e correções. |
| `/scans/success` | GET | Confirmar o upload e apresentar as digitalizações e correções. |
| `/answers/<id>/page.pdf` | GET | Consultar a página processada em PDF. |
| `/answers/<id>/page.png` | GET | Consultar a imagem processada. |
| `/scans/<id>/original.pdf` | GET | Descarregar o PDF original. |

A página inicial usa contagens reais da base de dados, calculadas por
`get_correction_summary()`, e apresenta atalhos para as funcionalidades do serviço.
O progresso corresponde às respostas corrigidas sobre o total de respostas;
uma nota zero também conta como correção. Sem digitalizações, apresenta um
convite para carregar o primeiro PDF. O layout adapta-se a ecrãs pequenos.

Os identificadores de exame e professor são introduzidos manualmente. Cada
par exame/questão tem uma atribuição, e cada professor só pode ficar responsável
por uma questão por exame. O mesmo professor pode participar noutros exames.
É possível trocar o professor enquanto não existir nenhuma resposta corrigida
para esse exame/questão. Depois da primeira correção, incluindo uma nota zero,
a troca é recusada para preservar a associação das notas ao professor original.
Repetir a mesma atribuição não altera os dados.
Só é possível guardar ou editar uma correção depois de atribuir um professor
ao respetivo par exame/questão. Sem atribuição, a listagem apresenta a opção
de atribuir professor. O acesso direto à rota de correção, por GET ou POST,
devolve uma mensagem com estado HTTP 409 e não permite guardar alterações.
Os PDFs e imagens das respostas continuam disponíveis para consulta.

As mensagens seguem o padrão do User Service: `get_error_message()` traduz os
códigos de erro num dicionário e o template recebe as variáveis `error` ou
`success`, sem `flash()`, sessões Flask ou `SECRET_KEY`.
Os erros mantêm o utilizador no formulário e preservam os campos preenchidos;
no upload, é necessário selecionar novamente o ficheiro.
Após sucesso, há um redirecionamento para a rota de confirmação correspondente.
No upload, `count` indica o número de páginas, por exemplo `/scans/success?count=4`.
A confirmação permanece após atualizar e pode ser aberta diretamente pelo URL.
As rotas normais das listagens não apresentam mensagens de sucesso.

## Processamento e correção

- Cada folha tem quatro questões, pela ordem 1–4. Os PDFs produzidos pelo
  Printing Service contêm exatamente `4 × n` páginas. Também se aceitam os
  exemplos de cinco páginas do ZIP, ignorando a primeira página introdutória
  (`4 × n + 1`). Outros totais são rejeitados.
- O PDF original é preservado. As páginas usadas para correção são guardadas
  como PDF e PNG, com os QR codes detetados cobertos a cinzento.
  Antes de ocultar os códigos, normaliza-se a rotação das páginas sem alterar
  a sua aparência, para que a cobertura também funcione em páginas rodadas.
  A deteção combina a página completa com regiões sobrepostas e deteção de
  códigos individuais, incluindo códigos repetidos que a análise global pode omitir.
- A nota deve ser finita e não negativa, sem máximo definido. O comentário é
  opcional. Guardar uma nota, incluindo zero, marca a resposta como corrigida.
  Não se calcula a nota final do exame.

O limite de upload é 50 MB. PDFs vazios, inválidos ou protegidos por password
são rejeitados. O upload é guardado numa única transação após o processamento.
Os IDs nos URLs devem ser inteiros positivos de 64 bits; valores fora desse
intervalo e registos inexistentes devolvem 404.

## Estrutura e dados

| Ficheiro | Conteúdo |
| --- | --- |
| `cs_app.py` | Configuração, rotas, validação e processamento dos PDFs. `split_pdf_into_pages()` mantém o exemplo fornecido; `cover_qr_codes_with_gray_squares()` reforça a deteção com regiões sobrepostas e códigos individuais. |
| `correction_data.py` | Modelos `ProfessorAssignment`, `ExamCorrectionPdf` e `QuestionCorrection`: atribuições, PDFs originais, páginas e correções. |
| `templates/` | Formulários, listagens e visualização das respostas. |

`CORRECTION_DATABASE_URL` permite alterar a ligação à base de dados.
Por omissão, usa-se o SQLite na pasta do serviço.

As novas bases incluem restrições de unicidade para os pares exame/questão e
exame/professor.

Cada resposta guarda apenas `sheet_number` e `question_number` para identificar
a sua posição. O trio upload/folha/questão é único. `page_number` é uma propriedade
calculada por `(sheet_number - 1) * 4 + question_number`, sem coluna na base de
dados. As listagens são ordenadas por upload, folha e questão.

Os testes usam bases temporárias e inicializam o módulo com uma base em memória,
sem criar nem modificar `correction_service/corrections.sqlite`. Incluem códigos
QR repetidos do Printing Service e verificam cada canto das páginas PDF e PNG.
