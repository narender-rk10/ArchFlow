/**
 * Technology logos for canvas nodes, from Simple Icons (CC0; see THIRD_PARTY_NOTICES.md). Hand-curated: a
 * logo shows only when a component's technology (or name) clearly names that product. Logos whose own terms
 * add conditions (non-commercial, attribution, custom) are left out. AWS, Azure and OpenAI logos are not
 * in Simple Icons. Order matters: more specific products come first ("Supabase Postgres" → Supabase).
 */
import {
  siAirbyte, siAnthropic, siApacheairflow, siApachecassandra, siApachekafka, siApachespark, siAuth0, siClaude, siClerk,
  siClickhouse, siCloudflare, siCockroachlabs, siDatabricks, siDatadog, siDjango, siDocker, siDynatrace, siElasticsearch,
  siFastapi, siFirebase, siFlask, siGithub, siGithubactions, siGitlab, siGo, siGooglebigquery, siGooglecloud,
  siGooglecloudstorage, siGooglegemini, siGooglepubsub, siGrafana, siGraphql, siHelm, siHuggingface, siJaeger,
  siKnative, siKong, siKubernetes, siLangchain, siLanggraph, siMariadb, siMetabase, siMinio, siMlflow, siMongodb,
  siMysql, siNatsdotio, siNeon, siNetlify, siNewrelic, siNextdotjs, siNginx, siNodedotjs, siOkta, siOllama,
  siOpensearch, siPostgresql, siPrometheus, siPython, siPytorch, siQdrant, siRabbitmq, siReact, siRedis, siScylladb,
  siSentry, siSnowflake, siSpring, siSqlite, siStripe, siSupabase, siTemporal, siTensorflow, siTerraform,
  siTraefikproxy, siTypescript, siUpstash, siVault, siVercel, siWhatsapp, siZendesk,
  type SimpleIcon,
} from "simple-icons";

const RULES: [SimpleIcon, RegExp][] = [
  [siUpstash, /\bupstash\b/], [siSupabase, /\bsupabase\b/], [siNeon, /\bneon\b/],
  [siPostgresql, /\bpostgre(s|sql)\b|\bpgvector\b/], [siRedis, /\bredis\b|\bvalkey\b/], [siApachekafka, /\bkafka\b/],
  [siMongodb, /\bmongo(db)?\b/], [siMariadb, /\bmariadb\b/], [siMysql, /\bmysql\b/], [siSqlite, /\bsqlite\b/],
  [siOpensearch, /\bopensearch\b/], [siElasticsearch, /\belasticsearch\b/], [siClickhouse, /\bclickhouse\b/],
  [siSnowflake, /\bsnowflake\b/], [siApachecassandra, /\bcassandra\b/], [siCockroachlabs, /\bcockroach(db)?\b/],
  [siScylladb, /\bscylla(db)?\b/], [siQdrant, /\bqdrant\b/], [siMinio, /\bminio\b/],
  [siFirebase, /\bfirebase\b|\bfirestore\b/], [siGooglebigquery, /\bbigquery\b/], [siGooglepubsub, /\bpub\s*\/\s*sub\b/],
  [siGooglecloudstorage, /^cloud storage$/], [siGooglegemini, /\bgemini\b/],
  [siAnthropic, /\banthropic\b/], [siClaude, /\bclaude\b/], [siHuggingface, /\bhugging\s?face\b/], [siOllama, /\bollama\b/],
  [siLanggraph, /\blanggraph\b/], [siLangchain, /\blangchain\b/], [siMlflow, /\bmlflow\b/], [siPytorch, /\bpytorch\b/],
  [siTensorflow, /\btensorflow\b/],
  [siKnative, /\bknative\b/], [siKubernetes, /\bkubernetes\b|\bk8s\b|\b(eks|aks|gke)\b/], [siHelm, /\bhelm\b/],
  [siDocker, /\bdocker\b/], [siNginx, /\bnginx\b/], [siTraefikproxy, /\btraefik\b/], [siKong, /\bkong\b/],
  [siCloudflare, /\bcloudflare\b/], [siVercel, /\bvercel\b/], [siNetlify, /\bnetlify\b/],
  [siAuth0, /\bauth0\b/], [siClerk, /\bclerk\b/], [siOkta, /\bokta\b/], [siVault, /\bhashicorp vault\b/],
  [siTerraform, /\bterraform\b/],
  [siRabbitmq, /\brabbitmq\b/], [siNatsdotio, /\bnats\b/], [siTemporal, /\btemporal\b/],
  [siStripe, /\bstripe\b/], [siWhatsapp, /\bwhatsapp\b/], [siZendesk, /\bzendesk\b/],
  [siPrometheus, /\bprometheus\b/], [siGrafana, /\bgrafana\b/], [siDatadog, /\bdatadog\b/], [siSentry, /\bsentry\b/],
  [siDynatrace, /\bdynatrace\b/], [siNewrelic, /\bnew\s?relic\b/], [siJaeger, /\bjaeger\b/],
  [siApacheairflow, /\bairflow\b/], [siApachespark, /\b(apache )?spark\b/], [siDatabricks, /\bdatabricks\b/],
  [siAirbyte, /\bairbyte\b/], [siMetabase, /\bmetabase\b/],
  [siNextdotjs, /\bnext\.?js\b/], [siReact, /\breact\b/], [siNodedotjs, /\bnode\.?js\b/], [siFastapi, /\bfastapi\b/],
  [siDjango, /\bdjango\b/], [siFlask, /\bflask\b/], [siSpring, /\bspring( boot)?\b/], [siGraphql, /\bgraphql\b/],
  [siTypescript, /\btypescript\b/], [siPython, /\bpython\b/], [siGo, /^go$|\bgolang\b|^go (service|api|worker)\b/],
  [siGithubactions, /\bgithub actions\b/], [siGithub, /\bgithub\b/], [siGitlab, /\bgitlab\b/],
  [siGooglecloud, /\bgoogle cloud\b|\bgcp\b|\bcloud run\b|\bvertex ai\b/],
];

export interface BrandIcon { title: string; path: string; hex: string; dark: boolean }

const cache = new Map<string, BrandIcon | null>();

/** The logo for a component, from its technology first, then its name. */
export function brandIcon(technology: string, name = ""): BrandIcon | null {
  const key = `${technology}\u0000${name}`;
  const hit = cache.get(key);
  if (hit !== undefined) return hit;
  let found: BrandIcon | null = null;
  for (const text of [technology, name]) {
    const t = text.trim().toLowerCase();
    if (!t) continue;
    const rule = RULES.find(([, re]) => re.test(t));
    if (rule) {
      const [icon] = rule;
      const n = parseInt(icon.hex, 16);
      const lum = (0.299 * (n >> 16) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255)) / 255;
      found = { title: icon.title, path: icon.path, hex: `#${icon.hex}`, dark: lum < 0.25 };
      break;
    }
  }
  cache.set(key, found);
  return found;
}
