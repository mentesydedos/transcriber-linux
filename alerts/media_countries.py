"""
alerts/media_countries.py — De dónde es cada medio que aparece en resultados
de GDELT, para mostrarlo en "top canales" (search_detail.html).

Dos fuentes de datos, de mejor a peor:
1. sourcecountry del DOC API (alerts/gdelt.py) -- GDELT ya lo calcula y nos
   lo manda gratis con cada artículo; antes se usaba solo para clasificar
   nacional/internacional y se descartaba. Es exacto.
2. DOMAIN_COUNTRY -- tabla curada a mano (arranca con los mismos dominios
   de SERIOUS_DOMAINS, ver alerts/gdelt.py) para cuando no hay
   sourcecountry (todo lo que viene de BigQuery/GKG, ver
   alerts/gdelt_bigquery.py, que no trae ese campo). Se amplía a mano
   conforme aparezcan dominios frecuentes sin país identificado -- ver
   country_for() y el fallback de terminación de dominio.

Nunca hay necesidad de una consulta extra por esto -- todo sale de datos
que ya se bajan o de tablas estáticas.
"""

# GDELT (DOC API) devuelve el nombre del país en inglés -- se traducen los
# más frecuentes para que no se mezclen idiomas en la misma columna de la
# interfaz (todo lo demás de la app está en español). Los que no estén
# aquí se muestran tal cual vienen (mejor un nombre en inglés que nada).
EN_COUNTRY_ES = {
    'mexico': 'México', 'united states': 'Estados Unidos', 'united kingdom': 'Reino Unido',
    'spain': 'España', 'argentina': 'Argentina', 'colombia': 'Colombia', 'chile': 'Chile',
    'peru': 'Perú', 'uruguay': 'Uruguay', 'brazil': 'Brasil', 'canada': 'Canadá',
    'france': 'Francia', 'germany': 'Alemania', 'italy': 'Italia', 'japan': 'Japón',
    'china': 'China', 'russia': 'Rusia', 'india': 'India', 'australia': 'Australia',
    'new zealand': 'Nueva Zelanda', 'venezuela': 'Venezuela', 'ecuador': 'Ecuador',
    'bolivia': 'Bolivia', 'paraguay': 'Paraguay', 'costa rica': 'Costa Rica',
    'panama': 'Panamá', 'guatemala': 'Guatemala', 'honduras': 'Honduras',
    'el salvador': 'El Salvador', 'nicaragua': 'Nicaragua',
    'dominican republic': 'República Dominicana', 'cuba': 'Cuba',
    'puerto rico': 'Puerto Rico', 'south africa': 'Sudáfrica', 'nigeria': 'Nigeria',
    'egypt': 'Egipto', 'kenya': 'Kenia', 'israel': 'Israel', 'turkey': 'Turquía',
    'saudi arabia': 'Arabia Saudita', 'united arab emirates': 'Emiratos Árabes Unidos',
    'south korea': 'Corea del Sur', 'philippines': 'Filipinas', 'thailand': 'Tailandia',
    'vietnam': 'Vietnam', 'indonesia': 'Indonesia', 'malaysia': 'Malasia',
    'singapore': 'Singapur', 'pakistan': 'Pakistán', 'qatar': 'Catar',
    'ireland': 'Irlanda', 'netherlands': 'Países Bajos', 'belgium': 'Bélgica',
    'switzerland': 'Suiza', 'austria': 'Austria', 'sweden': 'Suecia',
    'norway': 'Noruega', 'denmark': 'Dinamarca', 'finland': 'Finlandia',
    'poland': 'Polonia', 'portugal': 'Portugal', 'greece': 'Grecia',
    'ukraine': 'Ucrania',
}

# Tabla curada -- arranca de SERIOUS_DOMAINS (alerts/gdelt.py) más algunos
# dominios .mx frecuentes. Ampliar aquí conforme se detecten dominios
# frecuentes sin país (ver country_for).
DOMAIN_COUNTRY = {
    # Agencias de noticias
    'reuters.com': 'Reino Unido', 'apnews.com': 'Estados Unidos', 'efe.com': 'España',
    'afp.com': 'Francia', 'xinhuanet.com': 'China', 'tass.com': 'Rusia',
    'ansa.it': 'Italia', 'dpa-international.com': 'Alemania', 'kyodonews.net': 'Japón',
    'upi.com': 'Estados Unidos',
    # Prensa internacional en inglés
    'bbc.com': 'Reino Unido', 'bbc.co.uk': 'Reino Unido', 'aljazeera.com': 'Catar',
    'theguardian.com': 'Reino Unido', 'nytimes.com': 'Estados Unidos',
    'washingtonpost.com': 'Estados Unidos', 'ft.com': 'Reino Unido',
    'economist.com': 'Reino Unido', 'cnn.com': 'Estados Unidos',
    'bloomberg.com': 'Estados Unidos', 'wsj.com': 'Estados Unidos',
    'npr.org': 'Estados Unidos', 'time.com': 'Estados Unidos',
    'newsweek.com': 'Estados Unidos', 'usatoday.com': 'Estados Unidos',
    'independent.co.uk': 'Reino Unido', 'telegraph.co.uk': 'Reino Unido',
    'foxnews.com': 'Estados Unidos',
    # Prensa en español no mexicana
    'dw.com': 'Alemania', 'france24.com': 'Francia', 'elpais.com': 'España',
    'infobae.com': 'Argentina', 'elmundo.es': 'España', 'abc.es': 'España',
    'lavanguardia.com': 'España', 'elperiodico.com': 'España',
    'lanacion.com.ar': 'Argentina', 'clarin.com': 'Argentina',
    'latercera.com': 'Chile', 'emol.com': 'Chile', 'eltiempo.com': 'Colombia',
    'elespectador.com': 'Colombia', 'elcomercio.pe': 'Perú', 'elpais.com.uy': 'Uruguay',
    # Agregados tras revisar los dominios más frecuentes sin país
    # identificado en datos reales (ver backfill, 2026-09-17).
    'milenio.com': 'México', 'sdpnoticias.com': 'México', 'elimparcial.com': 'México',
    'lasillarota.com': 'México', 'reforma.com': 'México', 'reporteindigo.com': 'México',
    'lineadirectaportal.com': 'México', 'zetatijuana.com': 'México',
    'mvsnoticias.com': 'México', 'planoinformativo.com': 'México',
    'diarioportal.com': 'México', 'ambito.com': 'Argentina',

    # ── Pasada masiva sobre los ~940 dominios sin país (2026-09-17) ──
    # Solo se agregó lo identificable con confianza razonable por el
    # nombre del dominio (medio conocido, indicativo de llamada de EU,
    # ciudad/estado de EU inequívoco, o "edición país" de una red de
    # sitios tipo bignewsnetwork.com que nombra cada dominio por país).
    # Lo genuinamente ambiguo se dejó fuera a propósito -- mejor sin dato
    # que un dato falso.

    # Yahoo/CNN/otros portales grandes -- sede en EU
    'yahoo.com': 'Estados Unidos', 'aol.com': 'Estados Unidos', 'cnn.com': 'Estados Unidos',
    # Medios/agencias conocidos por nombre (no EU)
    'dailymail.com': 'Reino Unido', 'marca.com': 'España', 'as.com': 'España',
    'periodistadigital.com': 'España', 'prnoticias.com': 'España', 'murcia.com': 'España',
    'lasexta.com': 'España', 'elconfidencial.com': 'España', 'okdiario.com': 'España',
    'libertaddigital.com': 'España', 'granadahoy.com': 'España', 'larioja.com': 'España',
    'diariovasco.com': 'España', 'elcorreo.com': 'España', 'levante-emv.com': 'España',
    'elperiodicodearagon.com': 'España', 'elperiodicoextremadura.com': 'España',
    'elperiodicomediterraneo.com': 'España', 'lacronicabadajoz.com': 'España',
    'lavozdelanzarote.com': 'España', 'sevillaactualidad.com': 'España', 'telva.com': 'España',
    'estadiodeportivo.com': 'España', 'esdiario.com': 'España', 'menorca.info': 'España',
    'elidealgallego.com': 'España', 'xornalgalicia.com': 'España', 'elexpres.com': 'España',
    'elconfidencialdigital.com': 'España', 'elmercuriodigital.net': 'España',
    'elplural.com': 'España', 'infovaticana.com': 'España',
    'cadena3.com': 'Argentina', 'mdzol.com': 'Argentina', 'perfil.com': 'Argentina',
    'cronista.com': 'Argentina', 'republica.com': 'Argentina', 'eldestapeweb.com': 'Argentina',
    'diariopanorama.com': 'Argentina', 'diarioregistrado.com': 'Argentina',
    'canal26.com': 'Argentina', 'lapoliticaonline.com': 'Argentina', 'baenegocios.com': 'Argentina',
    'ellitoral.com': 'Argentina',
    'tvazteca.com': 'México', 'aristeguinoticias.com': 'México', 'unotv.com': 'México',
    'lopezdoriga.com': 'México',
    'nvinoticias.com': 'México', 'elceo.com': 'México', 'sopitas.com': 'México',
    'chilango.com': 'México', 'udgtv.com': 'México', 'elmanana.com': 'México',
    'imagentv.com': 'México', 'poblanerias.com': 'México', 'notisistema.com': 'México',
    'hoyxalapa.com': 'México', 'elorbe.com': 'México', 'elarsenal.net': 'México',
    'dossierpolitico.com': 'México', 'nacion321.com': 'México', 'e-consulta.com': 'México',
    'mediotiempo.com': 'México', 'semana.com': 'Colombia', 'noticiasrcn.com': 'Colombia',
    'elcolombiano.com': 'Colombia', 'bluradio.com': 'Colombia', 'lasillavacia.com': 'Colombia',
    'larepublica.net': 'Colombia', 'wradio.com': 'Colombia', 'as.com.co': 'Colombia',
    'univision.com': 'Estados Unidos', 'telemundo.com': 'Estados Unidos',
    'telemundopr.com': 'Puerto Rico', 'primerahora.com': 'Puerto Rico',
    'listindiario.com': 'República Dominicana', 'diariolibre.com': 'República Dominicana',
    'diariodigitalrd.com': 'República Dominicana', 'diariodecuba.com': 'Cuba',
    'cubaheadlines.com': 'Cuba', 'jamaica-gleaner.com': 'Jamaica',
    'listindiario.net': 'República Dominicana', 'prensalibre.com': 'Guatemala',
    'elsalvador.com': 'El Salvador', 'crhoy.com': 'Costa Rica', 'teletica.com': 'Costa Rica',
    'semanariouniversidad.com': 'Costa Rica', 'elcomercio.pe': 'Perú', 'deperu.com': 'Perú',
    'lahaine.org': 'España', 'lacaderadeeva.com': 'México',
    'euronews.com': 'Francia', 'dw.com': 'Alemania', 'rt.com': 'Rusia',
    'telesurtv.net': 'Venezuela', 'lapatilla.com': 'Venezuela', 'noticierodigital.com': 'Venezuela',
    'bbc.co.uk': 'Reino Unido', 'irishtimes.com': 'Irlanda', 'thenational.scot': 'Reino Unido',
    'index.hr': 'Croacia', 'politika.rs': 'Serbia', 'a1.ro': 'Rumania',
    'gazetanord-vest.ro': 'Rumania', 'stiripesurse.ro': 'Rumania', 'eoficial.ro': 'Rumania',
    'bnrnews.bg': 'Bulgaria', 'haksozhaber.net': 'Turquía', 'ensonhaber.com': 'Turquía',
    'yeniduzen.com': 'Chipre', 'azertag.az': 'Azerbaiyán', 'armenpress.am': 'Armenia',
    'baidu.com': 'China', 'qianlong.com': 'China', 'shanghainews.net': 'China',
    'timesofindia.indiatimes.com': 'India', 'indiatimes.com': 'India',
    'economictimes.indiatimes.com': 'India', 'hindustantimes.com': 'India', 'livemint.com': 'India',
    'news.webindia123.com': 'India', 'thehindubusinessline.com': 'India', 'jbhe.com': 'Estados Unidos',
    'mangalorean.com': 'India', 'khaama.com': 'Afganistán', 'bangkokpost.com': 'Tailandia',
    'manilatimes.net': 'Filipinas', 'freemalaysiatoday.com': 'Malasia', 'ihalla.com': 'Corea del Sur',
    'gamavision.com': 'Ecuador', 'teleamazonas.com': 'Ecuador', 'entornointeligente.com': 'Venezuela',
    'businessghana.com': 'Ghana', 'allafrica.com': 'Sudáfrica', 'chimpreports.com': 'Uganda',
    'independent.co.ug': 'Uganda', 'tribuneonlineng.com': 'Nigeria',
    'jpost.com': 'Israel', 'arabnews.com': 'Arabia Saudita', 'thepeninsulaqatar.com': 'Catar',
    'middleeastmonitor.com': 'Reino Unido',
    'winnipegfreepress.com': 'Canadá', 'windsorstar.com': 'Canadá', 'brandonsun.com': 'Canadá',
    'cp24.com': 'Canadá', 'therecord.com': 'Canadá', 'thepeterboroughexaminer.com': 'Canadá',
    'castanetkamloops.net': 'Canadá', 'hilltimes.com': 'Canadá',
}

# Dominios de EU (indicativo de llamada de emisora, o ciudad/estado
# inequívoco de EU) -- se agregan aparte por volumen, mismo criterio que
# arriba (solo lo identificable con confianza).
_US_DOMAINS = [
    'santafenewmexican.com', 'breitbart.com', 'latimes.com', 'laopinion.com', 'theepochtimes.com', 'nypost.com',
    'sandiegouniontribune.com', 'denverpost.com', 'cbsnews.com', 'capitalgazette.com',
    'billboard.com', 'albuquerqueexpress.com', 'abc7.com', 'wwe.com', 'wral.com', 'wlky.com',
    'wgal.com', 'wdbo.com', 'utahindependent.com', 'union-bulletin.com', 'twincities.com',
    'tucsonpost.com', 'timesleader.com', 'timescall.com', 'theusnews.com', 'thedailybeast.com',
    'texasguardian.com', 'tennesseedaily.com', 'sun-sentinel.com', 'standardspeaker.com',
    'sanfordherald.com', 'sanantoniopost.com', 'redlandsdailyfacts.com', 'record-bee.com',
    'republicanherald.com', 'pjmedia.com', 'pilotonline.com', 'pasadenastarnews.com',
    'oklahomacitysun.com', 'ocregister.com', 'orlandosentinel.com', 'nuevamujer.com',
    'newser.com', 'newjerseytelegraph.com', 'mynewsla.com', 'mymotherlode.com',
    'milwaukeesun.com', 'massachusettssun.com', 'lasvegassun.com', 'ladailypost.com',
    'ky3.com', 'kxel.com', 'kwch.com', 'kunr.org', 'kunm.org', 'ktxs.com', 'kten.com',
    'ktbb.com', 'ksut.org', 'ksnblocal4.com', 'ksfr.org', 'krwg.org', 'krro.com', 'kroc.com',
    'krmg.com', 'krforadio.com', 'krcgtv.com', 'kq98.com', 'kpbs.org', 'komu.com', 'kold.com',
    'koamnewsnow.com', 'kmuw.org', 'kmbc.com', 'klkntv.com', 'kimt.com', 'kicks99.com',
    'kfilradio.com', 'kfgo.com', 'keyt.com', 'keysnews.com', 'kesq.com', 'kelofm.com',
    'kdhlradio.com', 'kcbd.com', 'katv.com', 'katu.com', 'k1047.com', 'kvia.com', 'ktvz.com',
    'ksl.com', 'krgv.com', 'krdo.com', 'kcra.com', 'kob.com', 'koat.com', 'kfoxtv.com',
    'journal-advocate.com', 'investors.com', 'irvinetimes.com', 'highlandcountypress.com',
    'greeleytribune.com', 'gjsentinel.com', 'fortmorgantimes.com', 'forbes.com', 'eptrail.com',
    'dailypress.com', 'dailylocal.com', 'dailykos.com', 'dailycamera.com', 'courant.com',
    'coloradostar.com', 'cincinnatisun.com', 'cbs4local.com', 'cbs12.com',
    'canoncitydailyrecord.com', 'californiatelegraph.com', 'bostonstar.com', 'baltimoresun.com',
    'austinglobe.com', 'aspentimes.com', 'abc7chicago.com', '1057thepoint.com', '1011now.com',
    'yumasun.com', 'yellowhammernews.com', 'yakimaherald.com', 'y105fm.com', 'xtra99.com',
    'wyomingpublicmedia.org', 'wyomingnews.com', 'wyff4.com', 'wxii12.com', 'wxerfm.com',
    'wwmt.com', 'wwd.com', 'wwaytv3.com', 'wvtm13.com', 'wttf.com', 'wthitv.com', 'wtae.com',
    'wsls.com', 'wset.com', 'wror.com', 'writersdigest.com', 'wpsdlocal6.com', 'wpde.com',
    'wowt.com', 'wondradio.com', 'wokv.com', 'wogx.com', 'wmur.com', 'wmtram.com', 'wmgk.com',
    'wksn.com', 'wkml.com', 'wkjc.com', 'wjtn.com', 'wjrz.com', 'wjactv.com', 'wistv.com',
    'wisn.com', 'wincountry.com', 'willistonherald.com', 'wiky.com', 'wifc.com',
    'whittierdailynews.com', 'wgme.com', 'wgauradio.com', 'wfmd.com', 'westhawaiitoday.com',
    'westfaironline.com', 'wesh.com', 'wdtimes.com', 'wdez.com', 'wcvb.com', 'wbkb11.com',
    'wbez.org', 'wataugademocrat.com', 'washingtoninformer.com', 'wapt.com',
    'wahpetondailynews.com', 'wadk.com', 'wach.com', 'wabx.net', 'vermontbiz.com',
    'vanityfair.com', 'vaildaily.com', 'utilitydive.com', 'usmagazine.com', 'urbanmilwaukee.com',
    'upr.org', 'upnorthlive.com', 'unm.edu', 'underdogdynasty.com', 'ukiahdailyjournal.com',
    'troyrecord.com', 'tpr.org', 'tonyskansascity.com', 'thetruthaboutcars.com',
    'therockofrochester.com', 'theq.fm', 'thenewsherald.com', 'themorningsun.com',
    'theintelligencer.net', 'thegazette.com', 'thedigitalcourier.com', 'thedailyworld.com',
    'theconservativetreehouse.com', 'thebusinessjournal.com', 'theblaze.com', 'thebeatdfw.com',
    'theatlantic.com', 'texarkanagazette.com', 'telemundohouston.com', 'telemundodenver.com',
    'telemundodallas.com', 'telemundo51.com', 'telemundo40.com', 'telemundo52.com',
    'telemundoatlanta.com', 'telemundolasvegas.com', 'telemundochicago.com',
    'telemundoareadelabahia.com', 'telemundo47.com', 'telemundo33.com', 'telemundo31.com',
    'telemundo62.com', 'telemundo48elpaso.com', 'telemundonuevomexico.com', 'stardem.com',
    'space.com', 'southwestarkansasradio.com', 'southernminn.com', 'somdnews.com',
    'siouxlandnews.com', 'silvercityradio.com', 'si.com', 'sdbj.com', 'saltlakecitysun.com',
    'sandiegosun.com', 'rockymounttelegram.com', 'rfdtv.com', 'quickcountry.com', 'q1075.com',
    'prowrestling.net', 'politicalwire.com', 'politicaldog101.com', 'pittsburghstar.com',
    'phoenixherald.com', 'paradisepost.com', 'orlandoecho.com', 'oklahomastar.com',
    'ohiostandard.com', 'oann.com', 'nydailynews.com', 'nvdaily.com', 'newyorktelegraph.com',
    'newyorkstatesman.com', 'newstribune.com', 'newson6.com', 'newsitem.com',
    'newschannel9.com', 'newsargus.com', 'neworleanssun.com', 'nereview.com', 'nebraska.tv',
    'nbcnewyork.com', 'nbcnews.com', 'nbcdfw.com', 'nashvilleherald.com', 'nasa.gov',
    'myq105.com', 'mymixfm.com', 'myleaderpaper.com', 'myfox28columbus.com',
    'montereycountynow.com', 'midmichigannow.com', 'messenger-inquirer.com', 'mercurynews.com',
    'mentalfloss.com', 'mcall.com', 'mankatofreepress.com', 'mainlinemedianews.com',
    'mainemirror.com', 'magic983.com', 'mactech.com', 'macombdaily.com', 'macdailynews.com',
    'losangelespress.org', 'localnews8.com', 'legalinsurrection.com', 'lebanondemocrat.com',
    'lamag.com', 'kvia.com', 'koat.com', 'julioastillero.com', 'ixtapayzihuatanejo.com',
    'itechpost.com', 'irvinetimes.com', 'houstonpublicmedia.org', 'hottalkradio.com',
    'hotelnewsresource.com', 'hot96.com', 'hollywoodreporter.com', 'hngnews.com', 'hitsfm.net',
    'hipertextual.com', 'herefordtimes.com', 'heraldglobe.com', 'guampdn.com', 'gx94radio.com',
    'grenadachronicle.com', 'gratefulweb.com', 'grandrapidsmn.com', 'grandforksherald.com',
    'goskagit.com', 'freerepublic.com', 'foxwilmington.com', 'foxsanantonio.com', 'fox9.com',
    'fox4beaumont.com', 'fox35orlando.com', 'fox23.com', 'fox21online.com', 'fox13news.com',
    'fortune.com', 'forocoatza.com', 'fool.com', 'fmglobo.com', 'floridastatesman.com',
    'fitsnews.com', 'firstalert7.com', 'firerescue1.com', 'fdpradio.com', 'fairfieldsuntimes.com',
    'esquire.com', 'espndeportes.espn.com', 'ems1.com', 'edition.cnn.com', 'us.cnn.com',
    'deadlinedetroit.com', 'dawnofthedawg.com', 'dallassun.com', 'dailypioneer.com',
    'dailygazette.com', 'dailygalaxy.com', 'dailybulletin.com', 'dailybreeze.com', 'ctmirror.org',
    'coloradohometownweekly.com', 'collider.com', 'coast1009.com', 'clickorlando.com',
    'clickondetroit.com', 'chronicletimes.com', 'chicoer.com', 'chicagotribune.com',
    'channel3000.com', 'centralmaine.com', 'cecildaily.com', 'cbs6albany.com', 'casino.org',
    'broomfieldenterprise.com', 'bozemandailychronicle.com', 'boredpanda.com', 'bocanewsnow.com',
    'blackamericaweb.com', 'beach951.com', 'b975.com', 'b93radio.com', 'azbigmedia.com',
    'averyjournal.com', 'arkansasonline.com', 'arizonadailyindependent.com', 'aol.com',
    'americanthinker.com', 'akronlegalnews.com', 'abcnews.com', 'abcnews4.com',
    'abc7amarillo.com', 'abc6onyourside.com', 'abc17news.com', 'abc12.com', '995qyk.com',
    '963kklz.com', '963jackfm.com', '933thedrive.com', '92q.com', '927thevan.com',
    '927thedrive.net', '620ckrm.com', '4029tv.com', '1049thewolf.com', 'wrestlinginc.com',
    'cagesideseats.com', 'pauldavisoncrime.com', 'thecherrycreeknews.com', 'sandiegored.com',
    'dailypolitical.com', 'abqjournal.com', 'newsinamerica.com', 'daisyherrera.com',
    'wdtimes.com', 'khaama.com',
]
for _d in _US_DOMAINS:
    DOMAIN_COUNTRY.setdefault(_d, 'Estados Unidos')

# "Edición país" de una misma red de sitios (tipo bignewsnetwork.com) --
# el nombre del dominio nombra el país que cubre esa edición.
_COUNTRY_EDITION_DOMAINS = {
    'brazilsun.com': 'Brasil', 'russiaherald.com': 'Rusia', 'malaysiasun.com': 'Malasia',
    'singaporestar.com': 'Singapur', 'srilankasource.com': 'Sri Lanka',
    'kenyastar.com': 'Kenia', 'japanherald.com': 'Japón', 'jamaicantimes.com': 'Jamaica',
    'israelherald.com': 'Israel', 'indiagazette.com': 'India', 'hongkongherald.com': 'Hong Kong',
    'europesun.com': 'Bélgica', 'chinanationalnews.com': 'China', 'britainnews.net': 'Reino Unido',
    'zimbabwestar.com': 'Zimbabue', 'taiwansun.com': 'Taiwán', 'sydneysun.com': 'Australia',
    'surinametimes.com': 'Surinam', 'shanghainews.net': 'China', 'sierraleonetimes.com': 'Sierra Leona',
    'pakistantelegraph.com': 'Pakistán', 'nigeriasun.com': 'Nigeria', 'newzealandstar.com': 'Nueva Zelanda',
    'nepalnational.com': 'Nepal', 'myanmarnews.net': 'Birmania', 'middleeaststar.com': 'Catar',
    'malaysiasun.com': 'Malasia', 'iranherald.com': 'Irán', 'dominicanrepublicpost.com': 'República Dominicana',
    'chimpreports.com': 'Uganda', 'calcuttanews.net': 'India', 'bruneinews.net': 'Brunéi',
    'bssnews.net': 'Bangladés', 'bangladeshsun.com': 'Bangladés', 'azerbaijannews.net': 'Azerbaiyán',
    'australiannews.net': 'Australia', 'afghanistansun.com': 'Afganistán', 'afghanistannews.net': 'Afganistán',
    'philippinetimes.com': 'Filipinas', 'northkoreatimes.com': 'Corea del Norte',
    'thailandnews.net': 'Tailandia', 'vietnamtribune.com': 'Vietnam', 'trinidadtimes.com': 'Trinidad y Tobago',
    'greekherald.com': 'Grecia', 'haitisun.com': 'Haití', 'infohaiti.net': 'Haití',
    'caribbeanherald.com': 'Trinidad y Tobago', 'caribbeannewsdigital.com': 'Cuba',
    'newsroompanama.com': 'Panamá', 'oklahomastar.com': 'Estados Unidos',
    'arabherald.com': 'Emiratos Árabes Unidos', 'asiabulletin.com': 'Singapur',
    'africaleader.com': 'Kenia', 'globalsecurity.org': 'Estados Unidos',
}
for _d, _c in _COUNTRY_EDITION_DOMAINS.items():
    DOMAIN_COUNTRY.setdefault(_d, _c)

# Respaldo final por terminación de dominio (ccTLD) -- menos preciso (un
# .com puede ser de cualquier país) pero mejor que nada para lo que no
# esté en la tabla curada de arriba.
TLD_COUNTRY = {
    'mx': 'México', 'us': 'Estados Unidos', 'uk': 'Reino Unido', 'ar': 'Argentina',
    'es': 'España', 'co': 'Colombia', 'cl': 'Chile', 'pe': 'Perú', 'uy': 'Uruguay',
    'br': 'Brasil', 'ca': 'Canadá', 'fr': 'Francia', 'de': 'Alemania', 'it': 'Italia',
    'jp': 'Japón', 'cn': 'China', 'ru': 'Rusia', 'in': 'India', 'au': 'Australia',
    'nz': 'Nueva Zelanda', 've': 'Venezuela', 'ec': 'Ecuador', 'bo': 'Bolivia',
    'py': 'Paraguay', 'cr': 'Costa Rica', 'pa': 'Panamá', 'gt': 'Guatemala',
    'hn': 'Honduras', 'sv': 'El Salvador', 'ni': 'Nicaragua', 'do': 'República Dominicana',
    'cu': 'Cuba', 'pr': 'Puerto Rico', 'za': 'Sudáfrica', 'ng': 'Nigeria', 'eg': 'Egipto',
    'ke': 'Kenia', 'il': 'Israel', 'tr': 'Turquía', 'sa': 'Arabia Saudita',
    'ae': 'Emiratos Árabes Unidos', 'kr': 'Corea del Sur', 'ph': 'Filipinas',
    'th': 'Tailandia', 'vn': 'Vietnam', 'id': 'Indonesia', 'my': 'Malasia',
    'sg': 'Singapur', 'pk': 'Pakistán', 'qa': 'Catar', 'ie': 'Irlanda',
    'nl': 'Países Bajos', 'be': 'Bélgica', 'ch': 'Suiza', 'at': 'Austria',
    'se': 'Suecia', 'no': 'Noruega', 'dk': 'Dinamarca', 'fi': 'Finlandia',
    'pl': 'Polonia', 'pt': 'Portugal', 'gr': 'Grecia', 'ua': 'Ucrania',
}


def country_for(domain: str | None, sourcecountry: str | None = None) -> str | None:
    """País de un medio -- ver docstring del módulo para el orden de
    prioridad. None si no se pudo determinar por ningún camino (queda
    pendiente de agregar a la tabla curada a mano)."""
    if sourcecountry:
        key = sourcecountry.strip().lower()
        return EN_COUNTRY_ES.get(key, sourcecountry.strip())
    domain = (domain or '').strip().lower()
    if domain.startswith('www.'):
        domain = domain[4:]
    if not domain:
        return None
    if domain in DOMAIN_COUNTRY:
        return DOMAIN_COUNTRY[domain]
    # Subdominios de un medio ya curado -- "amp.milenio.com",
    # "cnnespanol.cnn.com", "english.elpais.com", "es-us.noticias.yahoo.com"
    # son ediciones/versiones del MISMO medio, no uno distinto. Buscar por
    # sufijo evita tener que enumerar cada subdominio posible a mano.
    for known, country in DOMAIN_COUNTRY.items():
        if domain.endswith('.' + known):
            return country
    tld = domain.rsplit('.', 1)[-1] if '.' in domain else ''
    return TLD_COUNTRY.get(tld)
