const targetURL = "https://v5.kaleido.guru/index.js";
const response = await $http.get(targetURL);
$done({ body: response.body });
