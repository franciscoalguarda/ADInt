# Admin Service

Aplicação Flask independente para agendar exames, inscrever alunos por CSV,
consultar inscrições e pedir a impressão das folhas de resolução.
Implementa AD_1–AD_3 e ST_1, sem autenticação nesta parte do projeto.

## Executar

Na raiz do projeto, com o ambiente virtual `venv`:

```powershell
.\venv\Scripts\python.exe -m pip install -r admin_service\requirements.txt
.\venv\Scripts\python.exe admin_service\as_app.py
```

Abrir <http://127.0.0.1:5002>. A base de dados SQLite é criada automaticamente
em `admin_service/admin.sqlite` e pertence exclusivamente a este serviço.

Configuração opcional:

- `ADMIN_DATABASE_URL`: ligação SQLAlchemy à base de dados.
- `ADMIN_PRINTING_RPC_URL`: endereço XML-RPC do Printing Service;
  por omissão, `http://127.0.0.1:5003`.

## Páginas e endpoints

| URL | Métodos | Função |
| --- | --- | --- |
| `/` | GET | Cartões de resumo, atalhos e guia de agendamento, inscrição e impressão. |
| `/exams/schedule` | GET, POST | AD_1: agendar um exame. |
| `/exams` | GET | Listar exames e aceder às ações de inscrição e impressão. |
| `/exams/success` | GET | Confirmar o agendamento e apresentar a listagem de exames. |
| `/exams/<exam_id>/enroll` | GET, POST | AD_2: inscrever alunos por CSV. |
| `/enrollments` | GET | ST_1: listar inscrições e respetivos exames. |
| `/enrollments/success` | GET | Confirmar a inscrição e apresentar a listagem. |
| `/exams/<exam_id>/order-printing` | POST | AD_3: pedir a impressão por XML-RPC. |
| `/exams/printing/success` | GET | Confirmar o pedido de impressão e apresentar os exames. |

A página inicial apresenta as contagens reais de exames e inscrições, cartões
para as ações principais e um guia das três etapas de preparação de um exame.
Sem exames, apresenta um convite para agendar o primeiro.

Os erros de agendamento e inscrição mantêm o utilizador no formulário.
Os campos de agendamento são preservados; no CSV, é necessário selecionar
novamente o ficheiro. Os erros de impressão apresentam a listagem de exames
na resposta ao POST, com as ações disponíveis para tentar novamente.

Após sucesso, há um redirecionamento para a rota de confirmação correspondente.
Na inscrição, `count` indica o número de alunos. As confirmações permanecem após atualizar,
podem ser abertas diretamente e não repetem a operação. As rotas normais
das listagens não apresentam mensagens de sucesso.

## Validação e dados

O agendamento exige uma unidade curricular com até 200 caracteres e uma data
igual ou posterior à data atual do servidor. O identificador do exame é gerado
pela base de dados. IDs de exame inexistentes ou fora do intervalo positivo
de 64 bits nos URLs devolvem HTTP 404.
Depois da data do exame, novas inscrições e pedidos de impressão são bloqueados;
no próprio dia, continuam permitidos. O bloqueio é aplicado no servidor e as
ações deixam de estar disponíveis na listagem. O acesso ao formulário de
inscrição de um exame passado devolve HTTP 409.

O ficheiro de inscrições deve ter extensão `.csv`, usar UTF-8 com ou sem BOM
e conter apenas uma coluna de identificação: `student_id` ou `student_identifier`.
Ficheiros que incluam ambas são rejeitados, mesmo que os valores coincidam.
Os cabeçalhos são normalizados
sem distinguir maiúsculas de minúsculas e devem ser únicos. Cada identificador
é um inteiro positivo até `9223372036854775807`. Colunas adicionais são aceites
quando declaradas no cabeçalho.

A importação é atómica: dados inválidos, IDs repetidos no ficheiro ou alunos
já inscritos nesse exame anulam todo o lote. As mensagens identificam a linha
quando existe um erro nos campos ou no identificador.
Os IDs fornecidos não são validados na base de dados desse serviço.

Os modelos `Exam` e `Enrollment` estão em `admin_data.py`. As chaves estrangeiras
são ativadas em cada ligação SQLite para impedir inscrições em exames
inexistentes. A remoção direta de um exame elimina as respetivas inscrições.
O esquema mantém os campos indicados no enunciado:

| Modelo | Campos |
| --- | --- |
| `Exam` | `id`, `date`, `course` |
| `Enrollment` | `exam_id`, `student_id` |

## Impressão por XML-RPC

O método `print_exam` recebe quatro argumentos: `exam_id` como texto decimal,
`exam_date` no formato `YYYY-MM-DD`, `course` como texto e `student_ids` como
lista de textos decimais. Os IDs continuam a ser inteiros no SQLite; o texto
permite transportá-los sem o limite de 32 bits dos inteiros XML-RPC.

É necessário inscrever alunos antes de pedir a impressão. Falhas de comunicação,
erros XML-RPC e rejeições do pedido apresentam mensagens em inglês. Após sucesso,
a aplicação apresenta uma confirmação padrão. Os PDFs podem ser consultados
no Printing Service, que gera quatro páginas por aluno, com um identificador
único em QR code por folha. Consultar `printing_service/README.md` para executar
esse serviço.

As chamadas XML-RPC usam um timeout de 30 segundos para as operações de rede,
tanto por HTTP como por HTTPS.

Inscrever alunos apenas guarda as inscrições, sem contactar o Printing Service.
A geração e a atualização do PDF exigem carregar em `Order printing`. Se um PDF
com dois alunos tiver oito páginas e forem inscritos mais dois, mantém as oito
páginas até ao próximo pedido de impressão; nesse pedido, passa a dezasseis.
As folhas e os QR codes anteriores são preservados. Repetir um pedido sem
alterações mantém o mesmo PDF.

O Printing Service pode estar desligado durante a inscrição. Para gerar ou
atualizar o PDF, é necessário que esteja disponível quando se carrega em
`Order printing`, antes de terminar a data do exame. Cada pedido usa as
inscrições atuais da base do Admin, incluindo após reiniciar o serviço.

Reiniciar ambos os serviços depois de atualizar o código.

## Testes

```powershell
.\venv\Scripts\python.exe -m unittest discover -s admin_service -v
```

Os testes usam bases de dados temporárias e simulam a comunicação XML-RPC.
Verificam validações, importação atómica, preservação dos campos, mensagens
sem cookies, confirmações, IDs de 64 bits e integridade das inscrições.
Também cobrem o bloqueio por data, a impressão apenas por pedido explícito,
a independência das inscrições face ao Printing e a preservação da estrutura
pedida para a base de dados. Os testes de integração entre
Admin e Printing estão em `printing_service/test_admin_printing.py` e são
executados com os testes do Printing Service.
